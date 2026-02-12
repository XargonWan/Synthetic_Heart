Rate limits regulate the number of requests you can make to the Gemini API
within a given timeframe. These limits help maintain fair usage, protect against
abuse, and help maintain system performance for all users.

[View your active rate limits in AI Studio](https://aistudio.google.com/usage?timeRange=last-28-days&tab=rate-limit)

## How rate limits work

Rate limits are usually measured across three dimensions:

- Requests per minute (**RPM**)
- Tokens per minute (input) (**TPM**)
- Requests per day (**RPD**)

Your usage is evaluated against each limit, and exceeding any of them will
trigger a rate limit error. For example, if your RPM limit is 20, making 21
requests within a minute will result in an error, even if you haven't exceeded
your TPM or other limits.

Rate limits are applied per project, not per API key. Requests per day (**RPD**)
quotas reset at midnight Pacific time.

Limits vary depending on the specific model being used, and some limits only
apply to specific models. For example, Images per minute, or IPM, is only
calculated for models capable of generating images (Nano Banana), but is
conceptually similar to TPM. Other models might have a token per day limit (TPD).

Rate limits are more restricted for experimental and preview models.

## Usage tiers

Rate limits are tied to the project's usage tier. As your API usage and spending
increase, you'll have an option to upgrade to a higher tier with increased rate
limits.

The qualifications for Tiers 2 and 3 are based on the total cumulative spending
on Google Cloud services (including, but not limited to, the Gemini API) for the
billing account linked to your project.

| Tier | Qualifications |
|---|---|
| Free | Users in [eligible countries](https://ai.google.dev/gemini-api/docs/available-regions) |
| Tier 1 | Full paid Billing account [linked to the project](https://ai.google.dev/gemini-api/docs/billing#enable-cloud-billing) |
| Tier 2 | Total spend: \> $250 and at least 30 days since successful payment |
| Tier 3 | Total spend: \> $1,000 and at least 30 days since successful payment |

When you request an upgrade, our automated abuse protection system performs
additional checks. While meeting the stated qualification criteria is generally
sufficient for approval, in rare cases an upgrade request may be denied based on
other factors identified during the review process.

This system helps maintain the security and integrity of the Gemini API platform
for all users.

## Gemini API rate limits

Rate limits depend on a variety of factors (such as your quota tier) and can be
viewed in Google AI Studio. As your tier and account status change over time, your
rate limits will automatically be updated.

[View your active rate limits in AI Studio](https://aistudio.google.com/usage?timeRange=last-28-days&tab=rate-limit)

Specified rate limits are not guaranteed and actual capacity may vary.

## Batch API rate limits

[Batch API](https://ai.google.dev/gemini-api/docs/batch-api) requests are subject to their own rate
limits, separate from the non-batch API calls.

- **Concurrent batch requests:** 100
- **Input file size limit:** 2GB
- **File storage limit:** 20GB
- **Enqueued tokens per model:** The **Batch enqueued tokens** table lists the maximum number of tokens that can be enqueued for batch processing across all your active batch jobs for a given model.

### Tier 1

| Model | Batch enqueued tokens |
| Text-out models |||||
|---|---|---|---|---|
| Gemini 3 Pro Preview | 5,000,000 |
| Gemini 3 Flash Preview | 3,000,000 |
| Gemini 2.5 Pro | 5,000,000 |
| Gemini 2.5 Pro TTS | 25,000 |
| Gemini 2.5 Flash | 3,000,000 |
| Gemini 2.5 Flash Preview | 3,000,000 |
| Gemini 2.5 Flash Image Preview | 3,000,000 |
| Gemini 2.5 Flash TTS | 100,000 |
| Gemini 2.5 Flash-Lite | 10,000,000 |
| Gemini 2.5 Flash-Lite Preview | 10,000,000 |
| Gemini 2.0 Flash | 10,000,000 |
| Gemini 2.0 Flash Image | 3,000,000 |
| Gemini 2.0 Flash-Lite | 10,000,000 |
| Gemini 3 Pro Image Preview 🍌 | 2,000,000 |

### Tier 2

| Model | Batch enqueued tokens |
| Text-out models |||||
|---|---|---|---|---|
| Gemini 3 Pro Preview | 500,000,000 |
| Gemini 3 Flash Preview | 400,000,000 |
| Gemini 2.5 Pro | 500,000,000 |
| Gemini 2.5 Pro TTS | 100,000 |
| Gemini 2.5 Flash | 400,000,000 |
| Gemini 2.5 Flash Preview | 400,000,000 |
| Gemini 2.5 Flash Image Preview | 400,000,000 |
| Gemini 2.5 Flash TTS | 100,000 |
| Gemini 2.5 Flash-Lite | 500,000,000 |
| Gemini 2.5 Flash-Lite Preview | 500,000,000 |
| Gemini 2.0 Flash | 1,000,000,000 |
| Gemini 2.0 Flash Image | 400,000,000 |
| Gemini 2.0 Flash-Lite | 1,000,000,000 |
| Gemini 3 Pro Image Preview 🍌 | 270,000,000 |

### Tier 3

| Model | Batch enqueued tokens |
| Text-out models |||||
|---|---|---|---|---|
| Gemini 3 Pro Preview | 1,000,000,000 |
| Gemini 3 Flash Preview | 1,000,000,000 |
| Gemini 2.5 Pro | 1,000,000,000 |
| Gemini 2.5 Pro TTS | 1,000,000 |
| Gemini 2.5 Flash | 1,000,000,000 |
| Gemini 2.5 Flash Preview | 1,000,000,000 |
| Gemini 2.5 Flash Image Preview | 1,000,000,000 |
| Gemini 2.5 Flash TTS | 4,000,000 |
| Gemini 2.5 Flash-Lite | 1,000,000,000 |
| Gemini 2.5 Flash-Lite Preview | 1,000,000,000 |
| Gemini 2.0 Flash | 5,000,000,000 |
| Gemini 2.0 Flash Image | 1,000,000,000 |
| Gemini 2.0 Flash-Lite | 5,000,000,000 |
| Gemini 3 Pro Image Preview 🍌 | 1,000,000,000 |

## How to upgrade to the next tier

The Gemini API uses Cloud Billing for all billing services. To transition from
the Free tier to a paid tier, you must first enable Cloud Billing for your
Google Cloud project.

Once your project meets the specified criteria, it becomes eligible for an
upgrade to the next tier. To request an upgrade, follow these steps:

- Navigate to the [API keys page](https://aistudio.google.com/app/apikey) in AI Studio.
- Locate the project you want to upgrade and click "Upgrade". The "Upgrade" option will only show up for projects that meet [next tier qualifications](https://ai.google.dev/gemini-api/docs/rate-limits#usage-tiers).

After a quick validation, the project will be upgraded to the next tier.

## Request a rate limit increase

Each model variation has an associated rate limit (requests per minute, RPM).
For details on those rate limits, see [Gemini models](https://ai.google.dev/models/gemini).

[Request paid tier rate limit increase](https://forms.gle/ETzX94k8jf7iSotH9)

We offer no guarantees about increasing your rate limit, but we'll do our best
to review your request.