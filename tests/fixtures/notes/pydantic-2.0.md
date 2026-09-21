## v2.0.0 (2023-06-30)

Pydantic V2 is a ground-up rewrite. Highlights:

* The core validation logic now lives in Rust, giving a 5-17x speed-up.
* `BaseSettings` has been removed from pydantic and now lives in the separate
  `pydantic-settings` package. Install it with `pip install pydantic-settings`.
* `parse_obj_as` and `schema_of` are deprecated in favour of `TypeAdapter`.
* `Model.dict()` has been renamed to `Model.model_dump()`; the old name still
  works but emits a deprecation warning.
* `Config` classes are replaced by `model_config` dicts.
* Bumped the minimum supported Python version to 3.7.

See the migration guide for the full list of changes.
