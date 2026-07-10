# Changelog

All notable changes to the `offboard` package are documented here. This project
follows [Semantic Versioning](https://semver.org).

## [0.1.5] — 2026-07-10

### Fixed
- The offer card no longer renders the raw decision `rationale` as its subtitle.
  That string is developer-facing audit data (`reason -> type … [EV=…, margin=…;
  mode]`) and was leaking into the customer-facing UI. The card now shows only
  the offer; the reason, rationale, economics and decision trace are logged via
  `console.debug("[offboard] offer authorized", …)` instead.

### Added
- `offerSubtext` option: an optional customer-facing line under the offer
  headline, for hosts that want a reassuring sentence. Off by default.

## [0.1.4] — 2026-07-10

### Fixed
- Modal message log now scrolls reliably. The scroll container was missing
  `min-height:0`, so on shorter viewports a tall in-chat offer card (headline +
  the Accept/Decline buttons) could be clipped by the modal with no way to
  scroll to the buttons. Purely a CSS fix; no API change.

## [0.1.3] — 2026-07-10

### Added
- Typed `intervention.params`. An accepted offer now carries a structured
  `params` handle — e.g. `{ stripe_coupon: "off50_3mo" }` — echoed back to
  `onAccept`, so you apply the save with a lookup into your billing system
  instead of parsing `description`. Optional and backward-compatible; existing
  integrations keep working unchanged.

## [0.1.2] — 2026-07-09

### Changed
- Packaging and publish-metadata maintenance release. No API changes.

## [0.1.1] — 2026-07-09

### Changed
- Default API base URL now points at the production host, so `Offboard.init()`
  connects out of the box with no `apiBaseUrl` override.
- Refreshed package keywords and the list of published files.

## [0.1.0] — 2026-07-08

### Added
- Initial release: the typed Offboard contract, a drop-in cancel-flow modal, and
  a session client for vanilla JS and React.

[0.1.3]: https://www.npmjs.com/package/offboard/v/0.1.3
[0.1.2]: https://www.npmjs.com/package/offboard/v/0.1.2
[0.1.1]: https://www.npmjs.com/package/offboard/v/0.1.1
[0.1.0]: https://www.npmjs.com/package/offboard/v/0.1.0
