<!-- Source URL: https://developers.pandascore.co/docs/rate-and-connections-limits.md
Retrieved: 2026-09-26 -->

---
updatedAt: 2025-09-16T00:28:04.000Z
---

Fetch the complete documentation index at: https://developers.pandascore.co/llms.txt. Use this file to discover all available pages before exploring further. Append .md to any documentation page URL to get its markdown version.

# Rate and connections limits

Learn how rate limits and maximum connections can affect your PandaScore integrations.

Usage of the PandaScore APIs is restricted depending on your plan. Your plan and your current usage are available on [your dashboard](https://app.pandascore.co/dashboard).

> For more information on plans, see [Pricing](https://pandascore.co/pricing).

## REST API

Usage of the REST API is restricted by a rate limit, i.e. a maximum number of requests per hour. In every API response, the number of remaining requests is available in the response `X-Rate-Limit-Remaining` HTTP header. Below is a table summary of the rate limit per hour for each plan:

<Table align={["left","left"]}>
  <thead>
    <tr>
      <th>
        Plan Name
      </th>

      <th>
        Rate Limit for the REST API
      </th>
    </tr>
  </thead>

  <tbody>
    <tr>
      <td>
        Schedules, Results & Context Data
      </td>

      <td>
        1k requests per hour
      </td>
    </tr>

    <tr>
      <td>
        Historical & Post-Match Data
      </td>

      <td>
        10k requests per hour
      </td>
    </tr>

    <tr>
      <td>
        Real-time Data (Basic)
      </td>

      <td>
        10k requests per hour
      </td>
    </tr>

    <tr>
      <td>
        Real-time Data (Pro)
      </td>

      <td>
        10k requests per hour
      </td>
    </tr>
  </tbody>
</Table>

> Read more: [REST API > Errors](https://developers.pandascore.co/docs/errors)

## WebSockets API

The WebSockets API is available to those on either the Real-time Data (Basic) or the Real-time Data (Pro) plans. Both of these plans allow a maximum of **3** simultaneous connections to a given match.

> Read more: [Live API > Disconnections](https://developers.pandascore.co/docs/disconnections)