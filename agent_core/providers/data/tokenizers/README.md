# Bundled OpenAI vocabularies

These files let local context estimates run without a network request or a
dependency on LiteLLM's package layout. `encodings.json` contains the patterns
and special tokens from OpenAI's `tiktoken_ext/openai_public.py`; the gzip files
contain the unmodified BPE rank files from:

- https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken
- https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken

The SHA-256 hashes of the decompressed files are respectively:

- `223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7`
- `446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d`

See `LICENSE.tiktoken` for the upstream license. Updating the Python package's
model-name mappings does not add a model's context limits or establish an
encoding for a model absent from those mappings.
