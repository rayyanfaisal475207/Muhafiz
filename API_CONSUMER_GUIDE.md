# Muhafiz Data API: Consumer Guide

The Muhafiz Data API provides secure, read-only access to PSRMS, CMS, PKM, and criminal-record data stored in Supabase. Consumers authenticate using the API base URL and an assigned API key; direct database access is not required.

## Authentication

Send your assigned API key in every data request (`/fir`, `/roznamcha`, `/cms`, `/pkm`, and `/criminal-records`). The `/health` endpoint does not require authentication:

```http
X-API-Key: 79UNGNJTZHdmBYoPuc_0H1gmgDGLI8A6qHrZfLmKa1o
```

Deployed API base URL:

```text
https://muhafiz.onrender.com
```


## API documentation

Interactive documentation is available at:

```text
https://muhafiz.onrender.com/docs
```

## Endpoints

### Health check

```bash
curl -sS "https://muhafiz.onrender.com/health"
```

### Get FIRs in bulk

`page` and `page_size` are required.

```bash
curl -sS "https://muhafiz.onrender.com/fir?page=1&page_size=100" \
  -H "X-API-Key: YOUR_API_KEY"
```

This returns complete FIR case objects. Each includes the core FIR record, its police station and district context, and related records:

```text
fir_section
fir_position
fir_accused
fir_witness
fir_investigating_officer
chalaan_dispatch
chalaan_outcome
cross_version
fir_zimni
fir_zimni_index
malkhana_register
weapon_register
```

Related fields are arrays. An empty array means no matching records exist for that FIR.

### Get one FIR

```bash
curl -sS "https://muhafiz.onrender.com/fir/FIR_ID" \
  -H "X-API-Key: YOUR_API_KEY"
```

Example:

```bash
curl -sS "https://muhafiz.onrender.com/fir/fir-429-26" \
  -H "X-API-Key: YOUR_API_KEY"
```

### Get FIRs changed since a timestamp

`page` and `page_size` are required.

```bash
curl -sS "https://muhafiz.onrender.com/fir?updated_since=2026-08-18T15:10:00Z&page=1&page_size=100" \
  -H "X-API-Key: YOUR_API_KEY"
```

Use an ISO 8601 UTC timestamp ending in `Z`. This returns complete FIR bundles where the main FIR record was updated after that timestamp. It is intended for incremental sync or RAG re-indexing.

### Get Roznamcha entries

`page` and `page_size` are required.

```bash
curl -sS "https://muhafiz.onrender.com/roznamcha?page=1&page_size=100" \
  -H "X-API-Key: YOUR_API_KEY"
```

To return records for one police station only:

```bash
curl -sS "https://muhafiz.onrender.com/roznamcha?police_station_id=STATION_ID&page=1&page_size=100" \
  -H "X-API-Key: YOUR_API_KEY"
```

Roznamcha records are ordered by `entry_date`, then `entry_number`.

### Get CMS complaints

`page` and `page_size` are required. `updated_since` is optional.

```bash
curl -sS "https://muhafiz.onrender.com/cms?page=1&page_size=100" \
  -H "X-API-Key: YOUR_API_KEY"
```

Each complaint includes a nested `complainant` object when the complaint's
`complainant_person_id` matches `person.id`.

Get one complaint:

```bash
curl -sS "https://muhafiz.onrender.com/cms/COMPLAINT_ID" \
  -H "X-API-Key: YOUR_API_KEY"
```

Fetch complaints changed since a timestamp:

```bash
curl -sS "https://muhafiz.onrender.com/cms?updated_since=2026-08-18T15:10:00Z&page=1&page_size=100" \
  -H "X-API-Key: YOUR_API_KEY"
```

### Get PKM applications

`page` and `page_size` are required. `updated_since` is optional.

```bash
curl -sS "https://muhafiz.onrender.com/pkm?page=1&page_size=100" \
  -H "X-API-Key: YOUR_API_KEY"
```

Each application includes the service-specific fields (`character_certificate`,
`driving_license`, `employee_registration`, `loss_report`,
`tenant_registration`, `vehicle_verification`, and `women_violence_report`),
with unavailable service records set to `null`. It also includes available
applicant, police-station, employee, tenant, and women-station references.

Get one application:

```bash
curl -sS "https://muhafiz.onrender.com/pkm/APPLICATION_ID" \
  -H "X-API-Key: YOUR_API_KEY"
```

Fetch applications changed since a timestamp:

```bash
curl -sS "https://muhafiz.onrender.com/pkm?updated_since=2026-08-18T15:10:00Z&page=1&page_size=100" \
  -H "X-API-Key: YOUR_API_KEY"
```

### Get criminal records

`page` and `page_size` are required. `updated_since` is optional.

```bash
curl -sS "https://muhafiz.onrender.com/criminal-records?page=1&page_size=100" \
  -H "X-API-Key: YOUR_API_KEY"
```

Get one record:

```bash
curl -sS "https://muhafiz.onrender.com/criminal-records/RECORD_ID" \
  -H "X-API-Key: YOUR_API_KEY"
```

Fetch records changed since a timestamp:

```bash
curl -sS "https://muhafiz.onrender.com/criminal-records?updated_since=2026-08-18T15:10:00Z&page=1&page_size=100" \
  -H "X-API-Key: YOUR_API_KEY"
```

## Windows commands

In Windows Command Prompt or PowerShell, use `curl.exe` to ensure the native
cURL command is used:

```powershell
curl.exe -sS "https://muhafiz.onrender.com/fir?page=1&page_size=100" -H "X-API-Key: YOUR_API_KEY"
```

Roznamcha example:

```powershell
curl.exe -sS "https://muhafiz.onrender.com/roznamcha?page=1&page_size=100" -H "X-API-Key: YOUR_API_KEY"
```

## Pagination

All list endpoints require both pagination query parameters:

```text
page       Required. The batch number, starting at 1.
page_size  Required. Number of records in the batch; from 1 to 100.
```

Examples:

```text
/fir?page=1&page_size=100
/fir?page=2&page_size=100
/roznamcha?page=1&page_size=100
/cms?page=1&page_size=100
/pkm?page=1&page_size=100
/criminal-records?page=1&page_size=100
```

The API fetches and returns only the requested batch. It does not return all records and then split them in the client.

Every list response contains:

```json
{
  "data": [],
  "meta": {
    "page": 1,
    "page_size": 100,
    "total": 30
  }
}
```

Start with `page=1`, then request `page=2`, `page=3`, and so on. Stop after receiving at least `meta.total` records. Requests without either `page` or `page_size` return a validation error.

## Incremental sync

The `/fir`, `/cms`, `/pkm`, and `/criminal-records` list endpoints accept
`updated_since`. `/roznamcha` does not currently support that filter.

1. First run: fetch all pages and store/index them.
2. Save the successful sync time in UTC.
3. Later: call the applicable endpoint with `updated_since=YOUR_SAVED_TIME&page=1&page_size=100`, page through all results, and replace or re-index each returned record or bundle.
4. Save a new sync time only after the new records have been processed successfully.

For FIRs, the filter applies to `fir.updated_at`. Changes made only to child records require database triggers that update the parent FIR's `updated_at` to be included in incremental results.

## Error responses

Invalid authentication and missing records use this JSON error shape, for example:

```json
{
  "error": "FIR fir-429-26 not found",
  "detail": null
}
```

Missing or invalid keys return `401`; an unknown resource ID returns `404`; and invalid or omitted required query parameters return `422`.
