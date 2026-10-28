---
name: numverify
description: "Use when validating phone numbers, checking number formats, or looking up country dialing codes via the NumVerify API. Provides phone number validation, carrier lookup, line type detection, and supported country listing."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [phone, validation, numverify, api, telecom, lookup]
    related_skills: []
---

# NumVerify Phone Number Validation

## Overview

NumVerify is a REST API by APILayer for validating phone numbers worldwide. It checks whether a number is valid, returns formatted versions (local/international/E.164), identifies the carrier, line type (mobile/landline/VoIP), and location. Supports 232 countries/territories.

**Base URL:** `https://apilayer.net/api`
**Auth:** Query parameter `access_key` with your API key.
**Env var:** `NUMVERIFY_API_KEY`

## When to Use

- Validate if a phone number is real/formatted correctly
- Look up carrier, line type, or location for a phone number
- Convert between national and international phone formats
- List supported countries and their dialing codes
- Bulk phone number hygiene before SMS/voice campaigns

## API Endpoints

### 1. Number Validation — `GET /validate`

Validates a single phone number and returns metadata.

**Query Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `access_key` | string | **yes** | API key |
| `number` | string | **yes** | Phone number (international or national format) |
| `country_code` | string | no | Two-letter ISO code (e.g. `US`, `SG`, `GB`). **Required** if number is in national format. |
| `format` | integer | no | Set to `1` for prettified JSON (debugging) |
| `callback` | string | no | JSONP callback function name |

**Response (200):**

```json
{
  "valid": true,
  "number": "+14158586273",
  "local_format": "4158586273",
  "international_format": "+14158586273",
  "country_prefix": "+1",
  "country_code": "US",
  "country_name": "United States of America",
  "location": "Novato",
  "carrier": "AT&T Mobility LLC",
  "line_type": "mobile"
}
```

**Response Fields:**

| Field | Type | Description |
|---|---|---|
| `valid` | boolean | Whether the number is valid |
| `number` | string | Original number submitted |
| `local_format` | string | National/local format |
| `international_format` | string | International E.164 format |
| `country_prefix` | string | Country calling code (e.g. `+1`) |
| `country_code` | string | Two-letter ISO country code |
| `country_name` | string | Full country name |
| `location` | string | City/location (may be absent) |
| `carrier` | string | Carrier name (may be absent) |
| `line_type` | string | `mobile`, `landline`, `voip`, etc. |

**Error Responses:**

| Code | Meaning |
|---|---|
| 400 | Missing or invalid parameters |
| 401/403 | Invalid or missing API key |
| 404 | Endpoint not found |

Error bodies follow: `{"success": false, "error": {"code": <int>, "type": "<string>", "info": "<string>"}}`

### 2. Countries — `GET /countries`

Returns all 232 supported countries with their dialing codes.

**Query Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `access_key` | string | **yes** | API key |

**Response (200):**

```json
{
  "US": {"country_name": "United States of America", "dialling_code": "+1"},
  "SG": {"country_name": "Singapore", "dialling_code": "+65"},
  "GB": {"country_name": "United Kingdom", "dialling_code": "+44"}
}
```

Keyed by two-letter ISO country code. Each value has `country_name` (string) and `dialling_code` (string, e.g. `+65`).

## Python Implementation

### Environment Setup

```bash
# Set the API key
export NUMVERIFY_API_KEY="your_api_key_here"
```

### Validate a Phone Number

```python
import os, requests

NUMVERIFY_API_KEY = os.environ.get("NUMVERIFY_API_KEY")
BASE_URL = "https://apilayer.net/api"

def validate_number(number: str, country_code: str = None) -> dict:
    """Validate a phone number via NumVerify API.
    
    Args:
        number: Phone number in international or national format.
        country_code: Two-letter ISO code (required for national format numbers).
    
    Returns:
        dict with validation results.
    
    Raises:
        ValueError: On API error or missing key.
    """
    if not NUMVERIFY_API_KEY:
        raise ValueError("NUMVERIFY_API_KEY environment variable not set")
    
    params = {"access_key": NUMVERIFY_API_KEY, "number": number}
    if country_code:
        params["country_code"] = country_code
    
    resp = requests.get(f"{BASE_URL}/validate", params=params)
    data = resp.json()
    
    if not data.get("success", True):
        err = data.get("error", {})
        raise ValueError(f"NumVerify API error: {err.get('info', err.get('type', 'unknown'))}")
    
    return data

# Example usage
result = validate_number("+14158586273")
print(f"Valid: {result['valid']}, Country: {result['country_name']}, Carrier: {result.get('carrier', 'N/A')}, Line: {result.get('line_type', 'N/A')}")

# National format — requires country_code
result = validate_number("4158586273", country_code="US")
```

### List Supported Countries

```python
def list_countries() -> dict:
    """Get all supported countries and their dialing codes.
    
    Returns:
        dict keyed by ISO country code, values have 'country_name' and 'dialling_code'.
    """
    if not NUMVERIFY_API_KEY:
        raise ValueError("NUMVERIFY_API_KEY environment variable not set")
    
    resp = requests.get(f"{BASE_URL}/countries", params={"access_key": NUMVERIFY_API_KEY})
    data = resp.json()
    
    if not data.get("success", True):
        err = data.get("error", {})
        raise ValueError(f"NumVerify API error: {err.get('info', err.get('type', 'unknown'))}")
    
    return data

# Example
countries = list_countries()
print(f"Supported countries: {len(countries)}")
for code, info in list(countries.items())[:5]:
    print(f"  {code}: {info['country_name']} ({info['dialling_code']})")
```

### Quick One-Liner (terminal)

```bash
# Validate a number from the command line
curl -s "https://apilayer.net/api/validate?access_key=$NUMVERIFY_API_KEY&number=+14158586273" | python3 -m json.tool
```

## Free vs Paid Plans

| Feature | Free | Paid |
|---|---|---|
| Monthly requests | 100 | Varies by plan |
| HTTPS | No (HTTP only) | Yes |
| Number type detection | Basic | Full |
| Carrier lookup | Limited | Full |
| Location lookup | Limited | Full |

**Important:** Free plan uses `http://` — paid plans use `https://`. The implementation above uses HTTPS; change to HTTP if on the free plan.

## Common Pitfalls

1. **National format without country_code.** If you pass `4158586273` without `country_code=US`, the API cannot determine which country the number belongs to and may return incorrect results or an error. Always provide `country_code` for national-format numbers.

2. **Free plan HTTPS mismatch.** The free tier does not support HTTPS. If you get SSL errors, switch the base URL to `http://apilayer.net/api`.

3. **Missing `success` field in success responses.** A successful 200 validation response does NOT include a `success` field — only error responses do. Check for the presence of `valid` instead.

4. **`location` and `carrier` may be absent.** Not all numbers have location or carrier data available. Always use `.get()` with a default rather than direct key access.

5. **Rate limiting.** Free plan is limited to 100 requests/month. For bulk validation, batch carefully and cache results.

6. **API key in URL.** The key is passed as a query parameter, not a header. Avoid logging full URLs that include the key.

## Verification Checklist

- [ ] `NUMVERIFY_API_KEY` environment variable is set
- [ ] Test with a known valid number (e.g. `+14158586273`)
- [ ] Test with an invalid number to confirm `valid: false` response
- [ ] Test national format with `country_code` parameter
- [ ] Verify HTTPS vs HTTP matches your plan tier
