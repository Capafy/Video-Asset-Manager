# Publishing this package

This directory contains the skill instructions, application source, tests,
and licensed fonts. Personal projects, generated media, credentials, logs,
and machine-generated Python caches are not part of the distribution.

## Keep local data out

- Keep generated projects and runtime data outside this source directory.
- Never include `.env` files, credentials, private configuration, logs,
  `__pycache__` directories, or `.pyc` files in a release.
- `.gitignore` helps prevent new accidental commits. It does not remove
  files already committed, and it does not filter a manually created ZIP.
- Before publishing, inspect the staged files with `git diff --cached`
  and `git diff --cached --name-only`. Review commit history if the
  repository previously contained private material.
- Git commit author names and email addresses are public in a public
  repository. Configure an appropriate author identity before committing;
  GitHub provides a noreply email option.

Environment-variable names in the source and synthetic test fixtures are
part of the implementation; they are not configured credential values.

## Third-party notices

The five bundled fonts are distributed under the SIL Open Font License 1.1.
Keep `assets/webapp/licenses/` with the fonts when copying or packaging them.
That directory records the official sources, file hashes, copyright notices,
and complete license texts. Font licenses do not apply to the application
source or to videos rendered with the fonts.

The two identified canvas-snapping and edge-scrolling functions have been
replaced with implementations written from functional specifications.
Historical source attribution and license terms are retained in
`assets/webapp/licenses/tooscut-NOTICE.md`. The surrounding PIP implementation
was reviewed against that upstream revision; no additional directly copied
block was identified in that scope.

The project owner confirmed ownership of the supplied UI template. OpenReel
and OpenCut were identified as implementation references; their verified MIT
notices are retained for any reused portions. See
`assets/webapp/licenses/editor-sources.md` for the source inventory.

Do not describe the whole package as MIT-licensed or fully license-cleared
on the basis of the font notices. No new repository-wide source-code
license is granted by this packaging cleanup.

## Runtime network boundary

The application defaults to port `4200` on `0.0.0.0` for forwarded-host use.
It has no built-in login or per-user project authorization. Anyone who can
reach the service may read, download, and modify its project data.

For local-only use, set `VAM_BIND=127.0.0.1`. For hosted use, place the
service behind an authenticated host gateway and restrict direct access
to its port. Do not expose it directly to an untrusted network. This
packaging cleanup does not change the runtime's binding or access model.
