# ADR-0025: Model calls go to a configured OpenAI-compatible endpoint

Every embedding plex-db-ex needs, of a frame or a phrase, is a `POST /v1/embeddings` to an OpenAI-compatible base URL naming the model in the request: `PLEXDB_FRAME_ENCODER_URL` for the writer, a URL its caller passes for the reader. Neither loads a model, holds a GPU, or decides when work runs; whatever serves the URL does. A slow answer is waited out, and a refused one marks the encoder unavailable.
