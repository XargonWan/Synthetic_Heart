<!-- source: https://ai.google.dev/gemini-api/docs/rate-limits -->

Try the new high-speed, cost-effective [Veo 3.1 Lite](/gemini-api/docs/models/veo-3.1-lite-generate-preview) model for video generation at scale. 

  * [ Home ](https://ai.google.dev/)
  * [ Gemini API ](https://ai.google.dev/gemini-api)
  * [ Docs ](https://ai.google.dev/gemini-api/docs)



Send feedback 

#  Rate limits

Rate limits regulate the number of requests you can make to the Gemini API within a given timeframe. These limits help maintain fair usage, protect against abuse, and help maintain system performance for all users.

[View your active rate limits in AI Studio](https://aistudio.google.com/rate-limit?timeRange=last-28-days)

## How rate limits work

Rate limits are usually measured across three dimensions:

  * Requests per minute (**RPM**)
  * Tokens per minute (input) (**TPM**)
  * Requests per day (**RPD**)



Your usage is evaluated against each limit, and exceeding any of them will trigger a rate limit error. For example, if your RPM limit is 20, making 21 requests within a minute will result in an error, even if you haven't exceeded your TPM or other limits.

Rate limits are applied per project, not per API key. Requests per day (**RPD**) quotas reset at midnight Pacific time.

Limits vary depending on the specific model being used, and some limits only apply to specific models. For example, Images per minute, or IPM, is only calculated for models capable of generating images (Nano Banana), but is conceptually similar to TPM. Other models might have a token per day limit (TPD).

Rate limits are more restricted for experimental and preview models.

## Usage tiers

Rate limits are tied to the project's usage tier. As your API usage and spending increase, you'll be automatically upgraded to a higher tier with increased rate limits.

The qualifications for Tiers 2 and 3 are based on the total cumulative spending on Google Cloud services (including, but not limited to, the Gemini API) for the billing account linked to your project.

Usage tier | Qualification | Billing tier cap  
---|---|---  
**Free** | [Active project](/gemini-api/docs/api-key#google-cloud-projects) or free trial | N/A  
**Tier 1** | Set up and link an active billing account | $250  
**Tier 2** | Paid $100 + 3 days from first successful payment | $2,000  
**Tier 3** | Paid $1,000 + 30 days from first successful payment | $20,000 - $100,000+  
  
While meeting the stated qualification criteria is generally sufficient for approval, in rare cases an upgrade request may be denied based on other factors identified during the review process.

This system helps maintain the security and integrity of the Gemini API platform for all users.

## Gemini API rate limits

Rate limits depend on a variety of factors (such as your usage tier) and can be viewed in Google AI Studio. As your tier and account status change over time, your rate limits will automatically update.

[View your active rate limits in AI Studio](https://aistudio.google.com/rate-limit?timeRange=last-28-days)

Specified rate limits are not guaranteed and actual capacity may vary.

## Priority inference rate limits

[Priority](/gemini-api/docs/priority-inference) consumption holds its own rate limits even though consumption is counted towards overall interactive traffic rate limits. **Default rate limits are: 0.3x the[standard rate limit](https://aistudio.google.com/rate-limit) for each model and tier**

## Batch API rate limits

[Batch API](/gemini-api/docs/batch-api) requests are subject to their own rate limits, separate from the non-batch API calls.

  * **Concurrent batch requests:** 100
  * **Input file size limit:** 2GB
  * **File storage limit:** 20GB
  * **Enqueued tokens per model:** The **Batch enqueued tokens** table lists the maximum number of tokens that can be enqueued for batch processing across all your active batch jobs for a given model.



### Tier 1

Model | Batch enqueued tokens  
---|---  
Text-out models  
Gemini 3.1 Pro Preview | 5,000,000  
Gemini 3.1 Flash-Lite Preview | 10,000,000  
Gemini 3 Flash Preview | 3,000,000  
Gemini 2.5 Pro | 5,000,000  
Gemini 2.5 Pro TTS | 25,000  
Gemini 2.5 Flash | 3,000,000  
Gemini 2.5 Flash Preview | 3,000,000  
Gemini 2.5 Flash Image Preview | 3,000,000  
Gemini 2.5 Flash TTS | 100,000  
Gemini 2.5 Flash-Lite | 10,000,000  
Gemini 2.5 Flash-Lite Preview | 10,000,000  
Gemini 2.0 Flash | 10,000,000  
Gemini 2.0 Flash Image | 3,000,000  
Gemini 2.0 Flash-Lite | 10,000,000  
Multi-modal generation models  
Gemini 3.1 Flash Image Preview 🍌 | 1,000,000  
Gemini 3 Pro Image Preview 🍌 | 2,000,000  
Embedding models  
Gemini Embedding | 500,000  
  
### Tier 2

Model | Batch enqueued tokens  
---|---  
Text-out models  
Gemini 3.1 Pro Preview | 500,000,000  
Gemini 3.1 Flash-Lite Preview | 500,000,000  
Gemini 3.1 Flash Preview | 400,000,000  
Gemini 2.5 Pro | 500,000,000  
Gemini 2.5 Pro TTS | 100,000  
Gemini 2.5 Flash | 400,000,000  
Gemini 2.5 Flash Preview | 400,000,000  
Gemini 2.5 Flash Image Preview | 400,000,000  
Gemini 2.5 Flash TTS | 100,000  
Gemini 2.5 Flash-Lite | 500,000,000  
Gemini 2.5 Flash-Lite Preview | 500,000,000  
Gemini 2.0 Flash | 1,000,000,000  
Gemini 2.0 Flash Image | 400,000,000  
Gemini 2.0 Flash-Lite | 1,000,000,000  
Multi-modal generation models  
Gemini 3.1 Flash Image Preview 🍌 | 250,000,000  
Gemini 3 Pro Image Preview 🍌 | 270,000,000  
Embedding models  
Gemini Embedding | 5,000,000  
  
### Tier 3

Model | Batch enqueued tokens  
---|---  
Text-out models  
Gemini 3.1 Pro Preview | 1,000,000,000  
Gemini 3.1 Flash-Lite Preview | 1,000,000,000  
Gemini 3.1 Flash Preview | 1,000,000,000  
Gemini 2.5 Pro | 1,000,000,000  
Gemini 2.5 Pro TTS | 1,000,000  
Gemini 2.5 Flash | 1,000,000,000  
Gemini 2.5 Flash Preview | 1,000,000,000  
Gemini 2.5 Flash Image Preview | 1,000,000,000  
Gemini 2.5 Flash TTS | 4,000,000  
Gemini 2.5 Flash-Lite | 1,000,000,000  
Gemini 2.5 Flash-Lite Preview | 1,000,000,000  
Gemini 2.0 Flash | 5,000,000,000  
Gemini 2.0 Flash Image | 1,000,000,000  
Gemini 2.0 Flash-Lite | 5,000,000,000  
Multi-modal generation models  
Gemini 3.1 Flash Image Preview 🍌 | 750,000,000  
Gemini 3 Pro Image Preview 🍌 | 1,000,000,000  
Embedding models  
Gemini Embedding | 10,000,000  
  
## How to upgrade to the next tier

To transition from the Free tier to a paid tier, you must first [set up billing in AI Studio](/gemini-api/docs/billing).

Once your project meets the specified criteria, it will be automatically upgraded to the next tier. Tier upgrades from the Free to Tier 1 will typically take effect instantly, and subsequent tier upgrades will take effect within 10 minutes. Navigate to the [Projects page](https://aistudio.google.com/projects) in AI Studio to check your tiers.

## Request a rate limit increase

Each model variation has an associated rate limit (requests per minute, RPM). For details on those rate limits, see the [AI Studio Rate Limit](https://aistudio.google.com/rate-limit) page.

[Request paid tier rate limit increase](https://forms.gle/ETzX94k8jf7iSotH9)

We offer no guarantees about increasing your rate limit, but we'll do our best to review your request.

Send feedback 
