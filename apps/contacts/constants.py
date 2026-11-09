"""Constants shared by the contacts app."""

# Upper bound of contacts a single bulk action may touch. Also keeps the comma-separated
# ID field of the bulk forms well under DATA_UPLOAD_MAX_MEMORY_SIZE.
MAX_BULK_SELECTION = 10_000
