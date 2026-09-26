<!-- Source URL: https://developers.pandascore.co/docs/authentication.md
Retrieved: 2026-09-26 -->

---
updatedAt: 2025-09-16T00:28:03.000Z
---

Fetch the complete documentation index at: https://developers.pandascore.co/llms.txt. Use this file to discover all available pages before exploring further. Append .md to any documentation page URL to get its markdown version.

# Authentication

Learn how to authenticate to PandaScore REST and WebSockets APIs.

Access to our APIs is restricted by token-based authentication. Your access token is available on your [Dashboard](https://app.pandascore.co/dashboard).

> 🚧 Warning
>
> This token is private, do not use it in client-side applications.

## REST API

All requests against the REST API must be authenticated. PandaScore accepts two authentication methods: via a Bearer token set in HTTP request headers or by passing the token in the URL.

**Bearer Token**

You can authenticate by setting an Authorization HTTP header on your request with the value `Bearer <insert your token here>`.

```curl cURL
curl --request GET \
     --url 'https://api.pandascore.co/videogames' \
     --header 'Accept: application/json' \
     --header 'Authorization: Bearer PLACEHOLDER_TOKEN_VALUE'
```

**URL Parameter**

Alternatively, you can also authenticate by passing your token via a `token` URL parameter.

```curl cURL
curl --request GET \
     --url 'https://api.pandascore.co/videogames?token=PLACEHOLDER_TOKEN_VALUE' \
     --header 'Accept: application/json' 
```

> These examples use the [All Video games > List video games](https://developers.pandascore.co/reference/get_videogames) endpoint.

## WebSockets API

The WebSockets API only accepts authentication via URL parameter. Append your token in a `token` URL parameter when trying to connect.

```shell Shell
wscat -c "wss://live.pandascore.co/matches/595466?token=PLACEHOLDER_TOKEN_VALUE" 
```

> 📘 Info
>
> This example uses [wscat](https://www.npmjs.com/package/wscat) to output WebSockets message in the terminal.