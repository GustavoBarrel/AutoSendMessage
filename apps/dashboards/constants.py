"""Shared limits of the custom dashboards."""

# Widest period (in days) a widget may look back. Same ceiling as the widget form, so the
# preview endpoint and stored filters cannot ask the database for absurd date ranges.
MAX_WIDGET_PERIOD_DAYS = 365
