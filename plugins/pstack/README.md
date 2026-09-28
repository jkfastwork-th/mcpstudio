# HIRDA Pstack Adapter

This plugin keeps Pstack as an engineering-methodology layer while HIRDA keeps
runtime, model, supervisor, fallback, quota, and machine authority.

The upstream portable Pstack tree is pinned as the `third_party/pstack-claude`
git submodule. The adapter reads workflow text from that exact revision instead
of copying Pstack logic into HIRDA core.

Supported proof workflows:

- `bug-fix` -> Pstack Bug fix playbook
- `feature` -> Pstack Feature playbook
- `review` -> Pstack `interrogate` skill

`route_task` selects and loads the methodology, then emits a coordinator
`hirda-cognitive-request-v1`. `build_role_request` translates Pstack roles into
HIRDA capability/depth hints. Neither tool emits `preferred_runtime`, so
semantic runtime ranking and Supervisor Policy remain authoritative.

Initialize the pinned upstream checkout after cloning:

```bash
git submodule update --init third_party/pstack-claude
```
