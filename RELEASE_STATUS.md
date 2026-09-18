# 0.4.3 GitHub release

The plugin includes the Node CLI and Python SDK source, so a separately published
npm package is not required for Connect. `npx --yes github:auteric-ai/auteric-kit
--localhost --store-url http://127.0.0.1:PORT` downloads and runs the repository
CLI for one session; it does not install a global executable.
Local integration is authorized by the connect request. Browser identity consent
is preserved; push, publication and production activation remain separate gates.

Validation evidence and known boundaries are recorded in the platform's
`docs/commerce/connect-acceptance.md`. The previous installed 0.3.0 plugin is not
silently replaced by editing this checkout. Install the reviewed local artifact
or publish the release only after approval.
