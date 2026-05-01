# sq1-platform

Shared cloud platform helpers for Square One Ceilings — used by every SQ1 cloud Function App and any local tool that needs the same secrets.

## What's in here

- **`Secrets`** — wraps Azure Key Vault (`sq1-platform-kv`) with caching + auth via Managed Identity (in Azure) or `az login` (local).
- **`QBOClient`** — QuickBooks Online OAuth client that handles rotating refresh tokens correctly. Persists each new refresh_token back to Key Vault atomically, so multiple consumers don't kill each other's chain.

## Architecture

```
                    ┌─────────────────────────┐
                    │   sq1-platform-kv       │
                    │   (Azure Key Vault)     │
                    └────────────┬────────────┘
                                 │
            ┌────────────────────┼────────────────────┐
            │                    │                    │
            ▼                    ▼                    ▼
    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
    │ Cloud Func A │    │ Cloud Func B │    │ Local Tools  │
    │ (Managed ID) │    │ (Managed ID) │    │ (az login)   │
    └──────────────┘    └──────────────┘    └──────────────┘
```

All three identity types use `DefaultAzureCredential`, which resolves to:
- Managed Identity when running on Azure App Service / Functions
- `az` CLI session on a developer's Mac
- Service principal env vars in CI

## Usage

### Just secrets

```python
from sq1_platform import Secrets

s = Secrets()
wojie_key = s.get("wojie-api-key")
client_secret = s.get("azure-client-secret")
```

### QBO (with rotation safety)

```python
from sq1_platform import QBOClient

qbo = QBOClient()

# Company info
company = qbo.get(f"companyinfo/{qbo.realm_id()}")
print(company["CompanyInfo"]["CompanyName"])

# All time activity (paginated automatically)
entries = qbo.list_time_activity()

# Custom query
result = qbo.query("SELECT * FROM Customer WHERE Active = true")
```

The `QBOClient` reads `qbo-refresh-token` always-fresh from Key Vault, exchanges it for an access_token, and **immediately writes the new refresh_token back to Key Vault before returning**. This is why both local tools and cloud Functions can share the same credentials without breaking each other.

## Installing

In an Azure Function or other Python project:

```toml
# requirements.txt
sq1-platform @ git+https://github.com/mikestl/sq1-platform.git@main
```

Or with pip:

```bash
pip install git+https://github.com/mikestl/sq1-platform.git@main
```

Locally for development:

```bash
git clone git@github.com:mikestl/sq1-platform.git
cd sq1-platform
pip install -e .[dev]
```

## Required Azure RBAC

The identity running this code needs:
- **Key Vault Secrets User** — for `Secrets.get()`
- **Key Vault Secrets Officer** — for `Secrets.set()` (used by `QBOClient` rotation)

For Managed Identity:
```bash
az role assignment create \
  --role "Key Vault Secrets Officer" \
  --assignee-object-id $PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --scope $(az keyvault show --name sq1-platform-kv --query id -o tsv)
```

## Secrets currently in `sq1-platform-kv`

| Name | Purpose | Rotates? |
|------|---------|----------|
| `azure-client-secret` | D365 service-principal secret | manual |
| `wojie-api-key` | Wojie/Dubber bearer token | manual |
| `qbo-client-id` | QBO app credential | manual |
| `qbo-client-secret` | QBO app secret | manual |
| `qbo-refresh-token` | QBO OAuth refresh token | **on every QBO call** |
| `qbo-realm-id` | QBO company ID | never |

## Why this exists

Before `sq1-platform`:
- Each cloud agent had its secrets in App Settings (plain-text in Azure)
- Local QBO MCP had its secrets in `~/Documents/quickbooks-mcp/.env` and `tokens.json`
- Refresh-token rotation between cloud and local wasn't coordinated → first one to use the token won, the other got `invalid_grant`

After:
- One Key Vault, every consumer reads through this library
- Rotation persisted atomically — chain stays intact
- New cloud agents inherit the pattern by importing the package
