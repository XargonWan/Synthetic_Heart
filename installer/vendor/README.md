# Vendored Windows binaries

Everything in this directory is placed here by CI. It is not checked in (see
`.gitignore`), and the installer works without it, just with fewer features.

## `pgvector/pg<major>/`

The vector extension for PostgreSQL, which semantic memory search needs.

pgvector ships **no prebuilt Windows binaries**: the GitHub repository has tags
(`v0.8.6` is current) but **zero releases**, so there is no asset to download.
Upstream's documented Windows path is to build it yourself with `nmake`. The
release workflow `.github/workflows/build-pgvector-windows.yml` does exactly
that and uploads the three files the extension needs:

```
pgvector/pg16/
    vector.dll            # from the build's lib/ (≈280 KB)
    vector.control        # from share/extension/
    vector--*.sql         # from share/extension/ - the whole upgrade chain, exactly
                          # the set CI stages (41 files for 0.8.6, not just one)
pgvector/pg17/
    ...
```

`scripts/install_prereqs.ps1` copies these into the provisioned PostgreSQL's
`lib/` and `share/extension/`. `scripts/bootstrap.py` then creates the extension
if it can, and if it cannot it says so and keeps SOUL in memory rather than
failing: a missing `vector.dll` must never stop SyntH from starting.

### Building it by hand

On Windows, with C++ support in Visual Studio installed, from an
**x64 Native Tools Command Prompt**:

```cmd
set "PGROOT=C:\path\to\synthest\pgsql"
cd %TEMP%
git clone --branch v0.8.6 https://github.com/pgvector/pgvector.git
cd pgvector
nmake /F Makefile.win
nmake /F Makefile.win install
```

`nmake install` writes into `%PGROOT%\lib` and `%PGROOT%\share\extension`, which
is exactly where the installer would have copied them. Nothing further is needed
on that machine; to ship it to other machines, copy those files into
`pgvector/pg<major>/` with the layout above.

`PGROOT` must be the PostgreSQL the installer ships, not any PostgreSQL: a DLL
built against a different major will not load. Take the same EDB binary archive
the prereqs script downloads (`postgresql-16.10-1-windows-x64-binaries.zip`) and
point `PGROOT` at where you unpacked it - it carries the headers and
`lib\postgres.lib` that `Makefile.win` needs. Verified against that archive with
`v0.8.6`: `CREATE EXTENSION vector` reports 0.8.6 and a distance query returns a
real number.

### Linux

Nothing to vendor. The distribution package is used instead
(`postgresql-16-pgvector` on Debian/Ubuntu, `pgvector` on Arch, `pgvector_16` on
RHEL), which `install.sh` installs.
