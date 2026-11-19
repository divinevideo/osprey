# MinIO integration fixture

The integration overlay builds the existing MinIO server and client from pinned
official source archives because their former `latest` container images are no
longer distributed. This fixture retains the same S3 API and bucket initialization
script; it does not select a replacement storage service.

- Server: [RELEASE.2025-10-15T17-29-55Z](https://github.com/minio/minio/releases/tag/RELEASE.2025-10-15T17-29-55Z),
  commit `9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a`.
- Client: [RELEASE.2025-08-13T08-35-41Z](https://github.com/minio/mc/releases/tag/RELEASE.2025-08-13T08-35-41Z),
  commit `7394ce0dd2a80935aded936b09fa12cbb3cb8096`.

Docker verifies each source archive's SHA-256 before extraction, builds with the
digest-pinned Go toolchain, verifies the upstream Go module checksums, and copies
the upstream AGPL license into each image. Updating a source pin requires updating
its archive checksum and validating the original integration suite, including
the execution-result storage round trip.

Both upstream repositories are archived. Use these builds only as disposable
integration fixtures; this is not a production storage or maintenance decision.
See the [upstream source-build instructions](https://github.com/minio/minio#install-from-source).
