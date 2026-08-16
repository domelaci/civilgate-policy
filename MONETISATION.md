# CivilGate — Monetisation Strategy

## Core principle
The data is not the product. Filtering, relevance, and alerts are the product.
Free tier builds trust and traffic. Paid tiers sell "not having to read the data yourself."

## Tiers

### Free (always)
- Public policy feed, scores, trend chart
- Basic region and category filters
- This is the credibility and discovery layer — must stay free

### Freemium (~£5–15/month, individuals)
- Email alerts for specific sectors or keywords
- Saved filters (e.g. "housing in the UK")
- Weekly digest email summarising changes in areas of interest

### Organisation (~£30–80/month)
- Multiple users per account
- Sector-specific dashboard (e.g. "environment NGO view")
- Shareable PDF snapshots for board reports and funding bids
- MP/representative voting history for a specific constituency
- Consultation deadline alerts ("14 days left to respond to this")

### Enterprise / Institutional (~£200–500/month)
- API access to scored policy data
- White-label dashboard for think tanks, law firms, consultancies
- Custom scoring weights (e.g. weight environmental score higher)
- Bulk data export for academic research

## Grant funding (parallel track, not monetisation)
Apply to these regardless of revenue progress — they buy runway:
- **NLnet Foundation** — nlnet.nl/propose (open calls, civic tech fits well)
- **Shuttleworth Foundation** — shuttleworthfoundation.org
- **Omidyar Network** — omidyar.com
- **Luminate** — luminategroup.com (specifically funds civic tech and transparency)
- **UK National Lottery Community Fund** — tnlcommunityfund.org.uk

## Features to build toward (in rough priority order)
1. User accounts + saved filters
2. Email alerts (sector/keyword triggered)
3. Weekly digest email
4. Consultation deadline detection ("Have your say" done properly — real open consultations with deadlines)
5. MP/representative voting record per constituency
6. PDF snapshot export
7. Organisation multi-user accounts
8. API access tier
9. Custom scoring weights per organisation

## Design principle for every new feature
Before building, ask: does this belong in the free tier (builds trust/traffic)
or the paid tier (saves time / adds relevance for a specific user)?
Never put relevance filtering behind a wall too early — it kills growth.

Keep this file updated as the strategy evolves. Reference it when making feature decisions.
