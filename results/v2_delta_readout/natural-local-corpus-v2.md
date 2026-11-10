# Natural Local-Edit OSS Corpus Benchmark v2

- Manifest SHA-256: `542b7bbabcfd2542c3fbedcddcb1c193fb59df1e9e8578abf2f76e47b04c49cb`
- Natural OSS commits evaluated: **`50`** across `click, packaging, platformdirs, tomlkit, werkzeug`
- Structural diversity (no nested-helper filter!): `50/50` non-nested functions/methods, `29` class methods, `32` with `>= 2` parameters
- Warm incremental `apply_write` wall median / p95 / max: **`5.73 ms` / `28.71 ms` / `36.17 ms`**
- Warm incremental `apply_write` CPU median / p95: **`6.64 ms` / `32.81 ms`** (`< 15 ms` gate: **`FAIL`**)
- Candidate warnings emitted: **`0`** across `0/50` commits (`0.0%` warning rate; median `0.0` bytes)

## Mechanical Selection Funnel by Repository

| Repo | HEAD SHA | Non-Merge Scanned | Not 1 Prod `.py` | `__init__`/`_version` | Sig/Symbol Changed | 0 or >1 Fn Body | Selected |
|---|---|---:|---:|---:|---:|---:|---:|
| `platformdirs` | `f9323b0d68` | 50 | 27 | 1 | 5 | 7 | **10** |
| `tomlkit` | `d7def7dbcc` | 28 | 11 | 1 | 3 | 3 | **10** |
| `packaging` | `ae4946b30f` | 72 | 41 | 4 | 8 | 9 | **10** |
| `click` | `2247b35ea1` | 42 | 19 | 0 | 10 | 3 | **10** |
| `werkzeug` | `2adaad1ab6` | 86 | 47 | 0 | 14 | 15 | **10** |

## All Evaluated Natural OSS Commits

| # | Repo | Commit | File (Pkg Files) | Changed Function (`params`) | Write Wall ms (p50) | Write CPU ms | Findings | Bytes |
|---:|---|---|---|---:|---:|---:|---:|---:|
| 1 | `platformdirs` | `3b870de8dc` | `platformdirs/api.py` (11) | `_unique` (1) | **4.71** | 5.47 | 0 | 0 |
| 2 | `platformdirs` | `3a5e7d0673` | `platformdirs/api.py` (11) | `PlatformDirsABC._optionally_create_media_directory` (2) | **5.24** | 5.47 | 0 | 0 |
| 3 | `platformdirs` | `9f5ff8a313` | `platformdirs/_xdg.py` (11) | `XDGMixin.user_runtime_dir` (1) | **1.87** | 2.34 | 0 | 0 |
| 4 | `platformdirs` | `3b4cf5f997` | `platformdirs/unix.py` (11) | `_runtime_dir_problem` (3) | **3.71** | 4.69 | 0 | 0 |
| 5 | `platformdirs` | `a3c9e1e6a1` | `platformdirs/testing.py` (11) | `isolated_dirs` (1) | **1.23** | 1.56 | 0 | 0 |
| 6 | `platformdirs` | `4e0605effa` | `platformdirs/__main__.py` (11) | `_dir_or_error` (2) | **0.52** | 0.78 | 0 | 0 |
| 7 | `platformdirs` | `dbe149743d` | `platformdirs/unix.py` (11) | `_get_user_dirs_folder` (1) | **3.21** | 2.34 | 0 | 0 |
| 8 | `platformdirs` | `43fef5f7cd` | `platformdirs/windows.py` (11) | `_is_fully_qualified` (1) | **3.57** | 3.91 | 0 | 0 |
| 9 | `platformdirs` | `870073ebd2` | `platformdirs/unix.py` (11) | `_resolve_user_dirs_value` (1) | **3.17** | 3.91 | 0 | 0 |
| 10 | `platformdirs` | `9b5c0fd49f` | `platformdirs/windows.py` (11) | `Windows.user_projects_dir` (1) | **3.58** | 3.91 | 0 | 0 |
| 11 | `tomlkit` | `d7def7dbcc` | `tomlkit/items.py` (12) | `Array.insert` (3) | **27.43** | 32.81 | 0 | 0 |
| 12 | `tomlkit` | `7ab0c794dd` | `tomlkit/api.py` (12) | `register_encoder` (1) | **1.68** | 2.34 | 0 | 0 |
| 13 | `tomlkit` | `004fce630d` | `tomlkit/api.py` (12) | `date` (1) | **1.88** | 3.91 | 0 | 0 |
| 14 | `tomlkit` | `18e7f98252` | `tomlkit/items.py` (12) | `Bool.__eq__` (2) | **26.90** | 30.47 | 0 | 0 |
| 15 | `tomlkit` | `229ee86608` | `tomlkit/container.py` (12) | `Container._validate_table_candidate` (3) | **14.09** | 17.97 | 0 | 0 |
| 16 | `tomlkit` | `446d6fab13` | `tomlkit/parser.py` (12) | `Parser._parse_number` (3) | **9.67** | 10.94 | 0 | 0 |
| 17 | `tomlkit` | `c07c0c15c4` | `tomlkit/source.py` (12) | `Source._to_linecol` (1) | **1.46** | 2.34 | 0 | 0 |
| 18 | `tomlkit` | `8c30fc17d9` | `tomlkit/items.py` (12) | `Array.__delitem__` (2) | **36.17** | 39.84 | 0 | 0 |
| 19 | `tomlkit` | `88aa4b1c11` | `tomlkit/items.py` (12) | `Array.as_string` (1) | **26.37** | 28.91 | 0 | 0 |
| 20 | `tomlkit` | `76d4714da0` | `tomlkit/items.py` (12) | `Float._new` (2) | **28.26** | 28.91 | 0 | 0 |
| 21 | `packaging` | `ae9d39c13b` | `packaging/pylock.py` (22) | `is_valid_pylock_path` (1) | **7.66** | 8.59 | 0 | 0 |
| 22 | `packaging` | `fa52d7237a` | `packaging/pylock.py` (22) | `_path_name` (1) | **8.02** | 9.38 | 0 | 0 |
| 23 | `packaging` | `d17cfaf0b7` | `packaging/_parser.py` (22) | `process_python_str` (1) | **2.74** | 3.12 | 0 | 0 |
| 24 | `packaging` | `022ae6c6fe` | `packaging/metadata.py` (22) | `_Validator._process_license_files` (2) | **8.35** | 10.16 | 0 | 0 |
| 25 | `packaging` | `23669b1b69` | `packaging/pylock.py` (22) | `Pylock.select` (6) | **12.95** | 14.84 | 0 | 0 |
| 26 | `packaging` | `9483cb8a74` | `packaging/markers.py` (22) | `_format_marker` (2) | **4.49** | 7.03 | 0 | 0 |
| 27 | `packaging` | `c567583804` | `packaging/tags.py` (22) | `platform_tags` (0) | **7.39** | 8.59 | 0 | 0 |
| 28 | `packaging` | `b80e6eb0a6` | `packaging/pylock.py` (22) | `Pylock.select` (6) | **9.31** | 12.50 | 0 | 0 |
| 29 | `packaging` | `4eb0753dba` | `packaging/metadata.py` (22) | `Metadata.from_raw` (3) | **9.46** | 10.94 | 0 | 0 |
| 30 | `packaging` | `826426fc88` | `packaging/pylock.py` (22) | `Pylock.select` (6) | **10.75** | 13.28 | 0 | 0 |
| 31 | `click` | `f67c2bb6c2` | `click/utils.py` (17) | `_make_default_short_help` (2) | **4.49** | 5.47 | 0 | 0 |
| 32 | `click` | `f25f697a43` | `click/core.py` (17) | `Command.format_arguments` (3) | **29.33** | 33.59 | 0 | 0 |
| 33 | `click` | `ce57b87ac4` | `click/termui.py` (17) | `edit` (6) | **6.26** | 7.03 | 0 | 0 |
| 34 | `click` | `1f9cd54f68` | `click/_termui_impl.py` (17) | `_tempfilepager` (2) | **7.96** | 11.72 | 0 | 0 |
| 35 | `click` | `f316d5cb3a` | `click/_termui_impl.py` (17) | `ProgressBar.render_finish` (1) | **7.50** | 8.59 | 0 | 0 |
| 36 | `click` | `a2ac5839f2` | `click/_termui_impl.py` (17) | `_tempfilepager` (2) | **7.46** | 7.81 | 0 | 0 |
| 37 | `click` | `047adef258` | `click/core.py` (17) | `Command.get_help_option_names` (2) | **28.71** | 32.03 | 0 | 0 |
| 38 | `click` | `c2ed41490b` | `click/testing.py` (17) | `CliRunner.isolated_filesystem` (2) | **5.60** | 7.81 | 0 | 0 |
| 39 | `click` | `8ee83ddbf5` | `click/testing.py` (17) | `CliRunner.isolated_filesystem` (2) | **5.86** | 7.03 | 0 | 0 |
| 40 | `click` | `fbdd434028` | `click/testing.py` (17) | `CliRunner.isolated_filesystem` (2) | **5.92** | 6.25 | 0 | 0 |
| 41 | `werkzeug` | `0d35bad79a` | `werkzeug/exceptions.py` (54) | `HTTPException.__init_subclass__` (2) | **5.04** | 4.69 | 0 | 0 |
| 42 | `werkzeug` | `21b45dbd33` | `werkzeug/wrappers/response.py` (54) | `Response.freeze` (1) | **6.27** | 5.47 | 0 | 0 |
| 43 | `werkzeug` | `818568f4e5` | `werkzeug/wrappers/response.py` (54) | `Response.make_sequence` (1) | **5.54** | 6.25 | 0 | 0 |
| 44 | `werkzeug` | `15fc22b699` | `werkzeug/wrappers/response.py` (54) | `Response.get_wsgi_headers` (2) | **4.73** | 5.47 | 0 | 0 |
| 45 | `werkzeug` | `06bf011ee0` | `werkzeug/wsgi.py` (54) | `wrap_file` (3) | **2.84** | 3.91 | 0 | 0 |
| 46 | `werkzeug` | `594452f6a4` | `werkzeug/routing/converters.py` (54) | `FloatConverter.to_url` (2) | **2.62** | 6.25 | 0 | 0 |
| 47 | `werkzeug` | `b2a2c09730` | `werkzeug/security.py` (52) | `safe_join` (2) | **1.82** | 1.56 | 0 | 0 |
| 48 | `werkzeug` | `d441b0c73b` | `werkzeug/utils.py` (54) | `send_file` (12) | **8.38** | 8.59 | 0 | 0 |
| 49 | `werkzeug` | `2c453bf3f3` | `werkzeug/middleware/proxy_fix.py` (54) | `ProxyFix.__call__` (3) | **1.31** | 1.56 | 0 | 0 |
| 50 | `werkzeug` | `54b7596dff` | `werkzeug/datastructures/accept.py` (53) | `Accept.from_header` (2) | **2.97** | 3.12 | 0 | 0 |
