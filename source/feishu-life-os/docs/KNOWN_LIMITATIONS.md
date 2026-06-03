# Known Limitations

- v2 is not yet the default legacy `/api/feishu/events` path.
- Real Feishu task/calendar sync methods are implemented, but payloads need one live OpenAPI verification pass with the user's granted permissions.
- Card callback endpoint resolves confirmations, but the card schema needs one live Feishu console verification pass.
- v2 Feishu image attachments can be downloaded and sent to the LM Studio vision path, but this still needs one live permission verification with the user's Feishu app.
- Weekly schedule parsing in `mock_provider` covers the validation phrase only; real use needs provider/prompt refinement.
- Postgres migration is not implemented for v2 schema yet.
- Legacy data and v2 data are separate.
- Bitable audit sync is staged and not yet writing v2 audit rows to a real Bitable table.
- Cloudflare quick tunnel URLs are temporary; Feishu URLs must be updated after tunnel restart unless a fixed Cloudflare domain is added.
