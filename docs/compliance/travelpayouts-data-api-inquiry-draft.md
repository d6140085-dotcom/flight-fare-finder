# Draft inquiry to Travelpayouts support (B6) — for owner review, NOT sent

Purpose: close item 1 of [search links spec §8.4](../m1.4-multi-platform-search-links.md).
The owner sends it from their own Travelpayouts account (support chat or ticket) after review.
Fill in the bracketed parts; don't add the API token or any personal data.

---

**Subject:** Data API — displaying reference fares alongside ordinary search links to other sites

Hello Travelpayouts team,

I run a small fare alert service for users in Taiwan ([project name / site URL]). Users subscribe to a route and departure month. We email them when
the cached fare we receive is at or below their target price.

**How we use your data today**

- We use only the **Aviasales Data API**: `/v1/prices/cheap`, moving to
  `/aviasales/v3/prices_for_dates`. We call it with our own token and cache results as you
  recommend.
- We do **not** use the Search API, and we do not combine your data with any other fare or
  metasearch API.
- Every price is labelled as a reference fare from Travelpayouts, for example: "Reference fare
  detected: NT$8,062 (source: Travelpayouts). Prices may have changed; the final price is shown
  on the booking site."
- The email always keeps a link to the itinerary on Aviasales.

**What we would like to add**

Next to the Aviasales link, we would like to offer ordinary public search links. These would
open the same route and dates on Skyscanner, Trip.com and Google Flights, so users can compare
on sites with a Traditional Chinese interface. These links:

- are plain links to each site's public search page, with no third-party affiliate or tracking
  parameters (we have none issued);
- never call those sites' APIs, and never send them our data;
- never state that the Travelpayouts fare is available on those sites.

**Questions**

1. Under the Travelpayouts Affiliate Agreement and the Data API terms, may we show Data API
   reference fares on the same email or page as these ordinary search links to Skyscanner,
   Trip.com and Google Flights?
2. If yes, are there conditions? For example, must the Aviasales link come first, or be the
   most prominent? Is specific attribution wording required? Are there limits on email use?
3. If not, which parts are not allowed? Is there an approved alternative? For example, would
   Trip.com links be acceptable through the Trip.com programme on Travelpayouts?
4. Is there anything else in how we use the Data API (described above) that we should change?

Thank you. A written answer we can keep on file would be appreciated.

[Name]
Travelpayouts account: [account email or partner ID — the marker is not secret, but include
only if needed]

---

Record the reply (date, ticket number, summary, and the decision it supports) in the spec's
§8.4 before enabling any non-Aviasales link. B6 stays **OPEN** until then.
