# Adaptation Config Registry

`ConfigRegistry` stores sensitive `SiteConfig` revisions beneath a private registry root. Callers receive immutable, non-sensitive `RegistryEntry` metadata; configuration selectors and fingerprint salts are returned only by the explicit `load()` API.

## Storage and crash guarantees

- Revisions are immutable, content-address-checked history. Revision publication is atomic and no-replace: POSIX links a durable anchored temporary file into its final name, while Windows uses write-through `MoveFileExW` without `REPLACE_EXISTING`. A pre-existing final name is compared under the registry lock and is never overwritten. A revision is durably committed before the safe manifest is replaced; if manifest replacement fails, the next open rebuilds it by scanning durable revisions.
- Writes use exclusive temporary files, file durability flushes, atomic replacement, and parent-directory durability where the platform supports directory handles.
- POSIX IO uses owner-only directories/files, `openat`-style directory descriptors, `O_NOFOLLOW`, same-descriptor `fstat` plus bounded reads, and directory `fsync` after mkdir, replace, and quarantine moves.
- Windows IO establishes DACLs through handle-level `SetSecurityInfo` and verifies a protected ACL containing exactly current-owner and SYSTEM full-control ACEs. Every operation holds trusted-root and ancestor directory handles opened with read/write sharing but no delete sharing, verifies canonical handle paths component by component, rejects reparse points, calls `FlushFileBuffers` for files, and uses write-through `MoveFileExW` for publication. Manifest and quarantine moves permit replacement where their protocols require it; revisions never do.
- If any permission, ACL verification, no-follow, or required durability operation fails, the registry operation fails closed.

## Recovery and quarantine

Recovery ignores temporary files, bounds scan entries/file counts/config bytes, and never trusts the manifest as the source of truth. Corrupt revisions, unknown schemas, broken digests, gaps, or conflicting history are quarantined. A history gap invalidates the full remaining chain rather than retaining a potentially misleading prefix.

Every quarantine attempt appends a uniquely named event containing only safe hashes, a reason identifier, revision name, UTC timestamp, and nonce. The source is moved with durable replacement to a unique `.bad` name, so an earlier event can never block a later one. Raw configuration content is not copied into event metadata or errors.

## Locking

Registry and per-config operations use bounded in-process locks plus operating-system file locks. File locks are opened as private regular files without following links. Process termination releases OS locks; callers receive `RegistryLockTimeout` when the configured timeout expires.
