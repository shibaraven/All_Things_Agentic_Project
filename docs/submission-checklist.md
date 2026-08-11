# Submission Checklist

## Ready

- [x] Competition README with problem, architecture, safety model, quickstart and disclosure
- [x] Hosted Command Center version 3 deployed at `shiftzero-command-center.mingjen.chatgpt.site`
- [x] Cloud Run API revision `shiftzero-api-00009-gvq` serving version `0.2.0-g3`
- [x] Five bounded Google ADK agents backed by Gemini 3.5 Flash on Vertex AI
- [x] Firestore, Pub/Sub, Model Armor, Cloud Trace, Secret Manager and IAM evidence
- [x] Architecture source plus `docs/architecture.png`
- [x] Four-minute recording script and shot list
- [x] Devpost-ready project copy
- [x] Pre-existing-work disclosure and MIT license
- [x] Five identical local rehearsals
- [x] Authenticated cloud three-event acceptance: 15/15 checks, 42/42 tasks, zero safety violations
- [x] Single clean local Git repository with the complete source and evidence pack

## Owner actions before final submission

- [ ] Approve changing the Sites access policy from owner-only to public/judge-accessible
- [ ] Create the public GitHub repository and push the prepared `main` branch
- [ ] Record the four-minute demo, upload it to YouTube or Vimeo, and insert the URL in `docs/devpost-copy.md`
- [ ] Replace `ADD_PUBLIC_GITHUB_URL` in `docs/devpost-copy.md`
- [ ] Open both public URLs in a signed-out/private window
- [ ] Paste the prepared copy and final links into Devpost, then submit

Do not publish a demo token, service-account credential, OAuth code, or raw secret output in the repository, video, or Devpost submission.
