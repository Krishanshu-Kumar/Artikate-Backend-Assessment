"""
Stand-in for a real third-party transactional email provider (SendGrid,
Postmark, SES, etc). Swap the body of `send()` for the real SDK call when
wiring this to an actual provider -- everything upstream (rate limiting,
retry, dead-lettering) is provider-agnostic and doesn't need to change.
"""

import random


class EmailProviderTemporaryError(Exception):
    """Raised for retryable failures (timeouts, 5xx, provider-side throttling)."""


class EmailProviderClient:
    def send(self, to_address: str, subject: str, body: str) -> None:
        # Simulated network call. In production this is e.g.:
        #   sendgrid.SendGridAPIClient(api_key).send(message)
        if random.random() < 0.02:  # ~2% simulated transient failure rate
            raise EmailProviderTemporaryError(f"provider timeout sending to {to_address}")
        # success: no-op in this stub