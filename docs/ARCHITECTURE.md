# Architecture

How s3-bagit moves bytes around without ever touching local disk.

## The constraint

Preservation bags are large — frequently tens of GB, sometimes hundreds
— and they live in S3 (AWS or a compatible service like Ceph RadosGW).
Workstations don't reliably have the free disk space to download a
bag, operate on it, and re-upload. The whole tool only makes sense
if both operations stream **S3 → S3**.

That single requirement drives most of the code shape below.

## Components

s3-bagit is a satellite of **s3-archive** ([DECISIONS.md](DECISIONS.md)
006), and the line between them is the main structural fact:

- **s3-archive** (runtime dependency, pinned to a git tag) owns
  everything that is about archives and S3 rather than BagIt: archive
  extraction and listing, member iteration for every supported
  format, S3 client and profile resolution, S3 URL parsing and format
  detection, and the streaming hash primitives (`s3_archive.hashing`).
- **s3-bagit** owns BagIt semantics: the RFC 8493 checks
  (`verify`, `verify_against`, and the checks the two share in
  `_bagit_checks`), bag creation (`create_bag`), and the CLI that turns
  results into reports and exit codes (`cli`, `_cli_parsers`).

The per-module file map is CLAUDE.md "Project Structure".

## Extract

Extraction is s3-archive's `extract`; s3-bagit calls it and adds the
BagIt part around it. The archive object is fetched from S3 as one
stream, handed to a streaming decoder that yields one member at a
time, and each member is pushed to S3 via `upload_fileobj`. The
decoder and the upload adapters that make non-seekable member streams
acceptable to boto3 live in s3-archive; see its `docs/ARCHITECTURE.md`.

```
        ┌──────────────────────────┐
        │ S3                       │
        │  s3://src/bag.tar.gz     │
        └────────────┬─────────────┘
                     │  get_object().Body
                     ▼
        ┌──────────────────────────┐
        │ tarfile (or stream_unzip)│
        │   r|gz mode — no seek    │
        └────────────┬─────────────┘
                     │  member-by-member
                     ▼
        ┌──────────────────────────┐
        │ upload_fileobj()         │
        │   per-member multipart   │
        └────────────┬─────────────┘
                     │
                     ▼
        ┌──────────────────────────┐
        │ S3                       │
        │  s3://dest/bag/...       │
        └──────────────────────────┘
```

Nothing is buffered between the decoder and the uploader except the
chunk of the current member being passed through.

What s3-bagit adds after the members land, in `cli._cmd_extract`:

- **Bag-root detection.** If the archive wraps the bag in one
  top-level directory, auto-verify is pointed at the nested bag root.
- **`--strip-top-level`.** Moves every `<dest>/<wrap>/<rel>` object
  to `<dest>/<rel>` with server-side copies plus deletes — no bytes
  flow through the workstation.
- **Auto-verify.** `verify_bag` runs on the landed bag unless
  `--no-verify` is given; it is also the integrity check on the
  strip-top-level moves.

## Create-bag

`create-bag` is the inverse of `extract`: it walks an S3 prefix and
emits a serialized BagIt `.tar.gz` at another S3 key. The same
"nothing on local disk" constraint applies. The challenge is that
BagIt manifests contain SHA-256 (or SHA-512) checksums of every
payload file, so we'd appear to need the bytes twice — once to hash,
once to write into the tar.

We avoid the second pass with two tricks:

1. **Tee the read**: each S3 GET is consumed by s3-archive's
   `HashingTap`, whose `read()` updates a `hashlib` hasher
   on the way through. `tarfile.addfile(info, fileobj)` reads exactly
   `info.size` bytes from `fileobj`, so the single S3 GET produces
   both the tar member bytes and the manifest checksum.
1. **Tag files trailing**: RFC 8493 places no ordering requirement
   on members within a serialized bag, so after all `data/` members
   are in the tar we append `bagit.txt`, `bag-info.txt`,
   `manifest-<algo>.txt`, and `tagmanifest-<algo>.txt` (built in
   memory from the accumulated digests).

The compressed tar output goes through an `os.pipe()` to a
single-worker thread pool that runs `client.upload_fileobj` against
the read end (`create_bag._tar_streamed_to_s3`):

```
        ┌──────────────────────────┐
        │ S3 list_objects_v2 +     │
        │ get_object per payload   │
        └────────────┬─────────────┘
                     │  body chunks
                     ▼
        ┌──────────────────────────┐
        │ HashingTap (tee)         │──► manifest digest
        └────────────┬─────────────┘
                     │  same bytes
                     ▼
        ┌──────────────────────────┐
        │ tarfile w|gz             │
        │  (data/* then tag files) │
        └────────────┬─────────────┘
                     │  os.pipe()
                     ▼
        ┌──────────────────────────┐
        │ worker thread:           │
        │ upload_fileobj(read_end) │
        └────────────┬─────────────┘
                     ▼
        ┌──────────────────────────┐
        │ S3                       │
        │  s3://dest/bag.tar.gz    │
        └──────────────────────────┘
```

Broken-pipe semantics make error propagation clean: if the uploader
dies mid-stream, the writer side sees `BrokenPipeError` on its next
flush. `_tar_streamed_to_s3` joins the upload in a `finally` through
`Future.result()`, so the upload's real cause is what is raised, with
the write-side `BrokenPipeError` riding along as its `__context__`.

## Verify

Verification is unavoidably **download + hash + compare** for every
file in the bag, because RFC 8493's correctness statement is "every
listed checksum matches the bytes that are there." There is no
metadata-only shortcut that gives the same correctness guarantee.

But "download + hash" is still streaming: each S3 object is read in
fixed-size chunks (s3-archive's `stream_hash_object`) into the
hashers, and the chunks are released immediately after being hashed.
A hasher holds only its algorithm-specific state (~kilobytes), not the
file's contents.

```
        ┌──────────────────────────┐
        │ S3                       │
        │  s3://bag/data/x.tif     │
        └────────────┬─────────────┘
                     │  get_object().Body
                     ▼
        ┌──────────────────────────┐
        │ for chunk in body:       │
        │   hasher.update(chunk)   │
        └────────────┬─────────────┘
                     │  hexdigest()
                     ▼
        ┌──────────────────────────┐
        │ compare to manifest line │
        │ collect mismatches       │
        └──────────────────────────┘
```

Verify is **single-pass per file**, whatever the number of manifest
algorithms: `_verify_files_single_pass` issues one GET per file and
feeds it to every algorithm whose manifest listed that file. The bag
is walked twice — payload files against the payload manifests, then
tag files against the tag manifests — and each file is read once.

## Verify-against

`verify-against` compares the files under an S3 prefix to the
manifests inside a *serialized* bag, without ever extracting it. It's
the cheapest way to confirm that a flat directory still matches its
archival `.tar.gz` — you avoid paying for a second copy on S3.

Mechanically it stitches together the streaming patterns from
`extract` and `verify`:

```
        ┌──────────────────────────┐
        │ S3 bag.tar.gz            │
        └────────────┬─────────────┘
                     │  one full stream
                     ▼
        ┌──────────────────────────┐
        │ iter_archive_members     │
        │  capture tag-file bytes  │
        │  drain payload bytes     │
        └────────────┬─────────────┘
                     │  manifest text(s) + bag-info.txt
                     ▼
        ┌──────────────────────────┐
        │ S3 target_prefix/        │
        │  list_objects_v2         │
        └────────────┬─────────────┘
                     │  per file
                     ▼
        ┌──────────────────────────┐
        │ get_object().Body        │
        │  → all required hashers  │
        │    (one read per file)   │
        └────────────┬─────────────┘
                     │  hexdigests
                     ▼
        ┌──────────────────────────┐
        │ compare to manifest line │
        │ collect mismatches       │
        └──────────────────────────┘
```

Two structural notes:

- **Bag root detection.** `verify-against` finds the single
  `bagit.txt` member in the archive and treats everything before its
  final segment as the wrapping directory (`""` if `bagit.txt` is at
  the archive top). Tag-file names are normalized against that root
  so a wrapped bag (`my-bag/manifest-sha256.txt`, what `create-bag`
  produces) and an unwrapped one parse identically.
- **Multi-algorithm bags are still single-pass per file.** For a bag
  with both `manifest-sha256.txt` and `manifest-sha512.txt`, each
  target file is read once and fed through both hashers by
  `stream_hash_object`. We do *not* re-issue `get_object` per
  algorithm.
- **Any supported archive format.** The archive is read through
  s3-archive's `iter_archive_members`, so every format `extract`
  accepts (README "Supported archive formats") works here too.

## Why "collect all errors" instead of fail-fast

When Preservation has a bag that doesn't verify, they want to know
*all* the things wrong with it in one run, not one mismatch per
invocation. The `BagVerifyResult` dataclass accumulates errors and
warnings; the CLI prints the full list at the end and exits 1 if
any errors were collected.

In `verify`, a missing `bagit.txt` or an unparseable manifest is
recorded as an error and the run carries on. A run stops early only where there is
nothing left to check: `verify` on a prefix with no objects, and
`verify_against` on an unreadable archive, no bag root, no payload
manifest, or an empty target prefix. HOWTO-SIMPLIFY.md "C901 ledger"
states the rule that keeps helpers from adding new early returns.

## What's deliberately NOT here

- **No local-disk fallback.** If you find yourself wanting one, the
  bag is probably small enough to use plain `aws s3 cp` + local
  `bagit-python`.
- **No multi-bag batching.** One bag per invocation. Operators who
  need batch behavior can wrap s3-bagit in a shell loop;
  parallelizing inside a single process would add complexity without
  buying much, because the bottleneck is S3 throughput.
- **No subprocess shelling out to s3cmd.** Everything goes through
  boto3 against the same endpoint. Operators inherit s3cmd's INI
  file for credentials, but s3cmd itself doesn't run.
