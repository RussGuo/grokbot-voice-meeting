# Examples

## Export context, then meet

```bash
mkdir -p contexts
cat > contexts/pm.md <<'EOF'
Offline sync is the top request in the notes we have.
Can commit:
- write the spec
EOF
cat > contexts/eng.md <<'EOF'
unknown: no latency numbers were exported.
EOF

meet --who pm,eng --topic "What is in the first milestone" --agenda "scope;risks" --context-dir contexts
```

The engineering brief will say the latency number is unknown. Do not fill it in.

## File-based follow-up while the meeting is live

```bash
export MEETING_PRINCIPAL=file
export MEETING_PRINCIPAL_DIR=runs/principal
# worker already running
# when outbox/pq-1.json appears:
echo '{"text":"Two weeks, not a promise of headcount."}' > runs/principal/inbox/pq-1.json
```

## Minutes back to the real bot

```bash
meet followup runs/20260101-120000
# send followups/eng.md to the engineering bot and wait for confirm / correct / retract
```
