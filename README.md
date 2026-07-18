# social-agent-platform

**This is the repo to clone.** Fleet Control dashboard. Network bots are **plugins** in their own repos.

```text
social-agent-platform/          <- you are here
├── agent-sdk/                  <- cloned from kashewknutt/agent-sdk
├── orchestrator/               Fleet Control UI (:7400)
├── plugins/
│   ├── bot-instagram/          <- kashewknutt/instagram-bot
│   ├── bot-linkedin/           <- kashewknutt/linkedin-bot
│   └── bot-x/                  <- kashewknutt/x-bot
├── setup.ps1 / setup.py
└── platform.manifest.yaml
```

## Quick start (Windows)

```powershell
cd D:\GitHub
git clone https://github.com/kashewknutt/social-agent-platform.git
cd social-agent-platform
.\setup.ps1 -Profile fleet
.\.venv\Scripts\Activate.ps1
python -m orchestrator_app.main
# http://127.0.0.1:7400
```

Profiles:

| Profile | What you get |
|---------|----------------|
| `core` | SDK + dashboard only |
| `instagram` | + Instagram plugin |
| `fleet` | + Instagram + LinkedIn + X plugins |

## Plugin repos

| Bot | Repo | Port |
|-----|------|------|
| Instagram | [instagram-bot](https://github.com/kashewknutt/instagram-bot) | 7411 |
| LinkedIn | [linkedin-bot](https://github.com/kashewknutt/linkedin-bot) | 7412 |
| X | [x-bot](https://github.com/kashewknutt/x-bot) | 7413 |

Shared control contract: [agent-sdk](https://github.com/kashewknutt/agent-sdk).

To add another bot: publish its own repo, add a `kind: plugin` entry under `packages:` in `platform.manifest.yaml`, and reference it from a profile.
