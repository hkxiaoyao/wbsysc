# Deployment requirements

- Merge the reviewed feature branch into `main` without force-pushing.
- Wait for the `main` GitHub Actions image build to publish `ghcr.io/hkxiaoyao/wbsysc:latest`.
- Use an independent migration account and run migrations in `004` → `005` → `006` order before starting the new image.
- Preserve database backups and fail before application startup if migration or image pull fails.
- Verify production health and retain a rollback path to the previous image.
- Archive this CCG task after deployment verification.
