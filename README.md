# jar.yazi

Walk through a local JAR in Yazi as if it were a directory. Open an inner file through your normal Yazi opener without unpacking the complete archive.

`jar.yazi` is strictly read only. It never repacks or mutates the source JAR.

## Requirements

* Yazi 26.9.1 or newer
* Python 3.10 or newer

Custom VFS providers are experimental in Yazi. A future Yazi release may require a matching plugin update.

## Install

```sh
ya pkg add gabber235/jar
```

Register the mount provider in `~/.config/yazi/vfs.toml`:

```toml
[jar.local]
kind = "mount"
run = "jar"
```

Replace smart enter in `~/.config/yazi/keymap.toml`:

```toml
[[mgr.prepend_keymap]]
on = [ "l" ]
run = "plugin jar smart-enter"
desc = "Enter directories and JARs, or open files"
```

Pressing `l` now has four outcomes:

* A directory is entered.
* A local `.jar` file is mounted and entered.
* A JAR entry is materialized lazily, then opened normally.
* Every other local file is opened normally.

The plugin also exposes an explicit mount command:

```toml
[[mgr.prepend_keymap]]
on = [ "g", "j" ]
run = "plugin jar mount"
desc = "Enter hovered JAR"
```

## Opening inner files

The plugin extracts the selected entry into Yazi runtime storage, preserving a safe file extension for opener matching. It then passes that local copy to Yazi's normal opener configuration. Only the selected entry is materialized.

An inner JAR opens as a file. Nested JAR traversal is outside version 1.

Editing a materialized inner file changes only the disposable copy. It never changes the source JAR.

## Optional class decompiler

The provider returns `.class` bytes without choosing a decompiler. You can add a command named `cfr` to `PATH`, then configure an opener:

```toml
[opener]
decompile = [
    { run = 'cfr %s1 | ${PAGER:-less}', block = true, for = "unix" },
]

[open]
prepend_rules = [
    { url = "*.class", use = "decompile" },
]
```

This recipe is optional. CFR installation and licensing remain separate from `jar.yazi`.

## Safety model

JAR input is untrusted. The Python backend owns validation and extraction:

* Absolute paths, dot segments, backslashes, empty segments, excessive depth, and excessive path length are rejected.
* Duplicate normalized paths and file versus directory collisions are rejected.
* ZIP symlinks are exposed as ordinary file payloads and are never followed.
* Encrypted and oversized entries are rejected.
* Extraction writes to a hash named temporary file in Yazi runtime storage, verifies length and CRC, then renames atomically.
* Every source change creates a new archive fingerprint. Metadata and content from different snapshots are never mixed.

The fixed version 1 limits are 100000 expanded entries, 256 MiB per file, 1024 bytes per path, and 64 path segments.

## Uninstall

Remove the `jar.local` table and the plugin keymaps from your Yazi configuration, then run:

```sh
ya pkg delete gabber235/jar
```

## Development

Run the backend tests:

```sh
python3 -m unittest discover -s tests
```

Check Lua formatting:

```sh
stylua --check main.lua
```

The backend uses only the Python standard library.

## License

MIT
