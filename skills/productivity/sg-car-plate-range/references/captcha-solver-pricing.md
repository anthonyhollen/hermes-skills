# CAPTCHA-Solving Services — Pricing Comparison

Researched June 2026. All services are **pay-per-solve** — **none offer a free tier**.

## Pricing (per 1,000 solves)

| Service | reCAPTCHA v2 | reCAPTCHA v3 | reCAPTCHA v2 Enterprise | reCAPTCHA v3 Enterprise | Min top-up |
|---------|-------------|-------------|------------------------|------------------------|------------|
| **2Captcha** | $1.00–2.99 | $1.45–2.99 | $1.00–2.99 | — | ~$3 |
| **CapSolver** | **$0.80** | $1.00 | $1.00 | $3.00 | ~$5 |
| **Anti-Captcha** | ~$1–3 | ~$1.5–3 | ~$1–3 | — | ~$3 |

**CapSolver is cheapest** at $0.80/1k for reCAPTCHA v2.

## For LTA VRL use case

The VRL page uses reCAPTCHA v2 (invisible). A single scrape attempt needs 1 solve. For weekly plate-range checks, cost is negligible (~$0.001/check).

## Integration pattern

1. Load page → extract `data-sitekey` from reCAPTCHA iframe
2. POST sitekey to solver API → get task ID
3. Poll task ID until solved → get `g-recaptcha-response` token
4. Inject token into page's reCAPTCHA callback → submit form

## Service URLs

- 2Captcha: https://2captcha.com/pricing — API docs: https://2captcha.com/api-docs
- CapSolver: https://capsolver.com/pricing — API docs: https://docs.capsolver.com
- Anti-Captcha: https://antcpt.com — API docs: https://anti-captcha.com/apidoc

## Notes

- 2Captcha has Python SDK on GitHub: `pip install 2captcha-python`
- CapSolver has the fastest solve times advertised (<5s for reCAPTCHA v2)
- All three support reCAPTCHA v2, v3, Enterprise, hCaptcha, FunCaptcha
- Balance is prepaid — top up via crypto, PayPal, or credit card
