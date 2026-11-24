# Private saved-trial correspondence export

Export correspondence from a saved `.eval` file or a native trial directory containing `artifacts/world/`:

```bash
uv run python -m refund_eval.runtime.correspondence /path/to/trial.eval \
  --out "$HOME/refund-eval-data/private-correspondence/new-export"
```

The output directory must not already exist and must pass the repository's private-output check. It contains `correspondence.json` (the structured private archive) and `correspondence.md` (a readable companion). Native world-only partial artifacts are supported without an agent `.eval`; the event log is retained when the final world is unavailable, but coverage is marked unavailable. When both nonempty saved World and event sources are present, coverage is `not_verified`: source presence does not prove complete historical capture. The exporter reads saved artifacts only and makes no model, mail, web, or trial calls.

The archive preserves saved identities and correspondence bodies; it is not a redacted sharing copy. Full linked `InteractionRecord` provenance, including context and action fields, is retained with the related correspondence records. Standalone company case collections, claims, offers, transfers, disclosures, follow-ups, wakeups, token ledgers, and other non-correspondence world state are omitted, as are non-correspondence event payloads; those omissions are listed in coverage metadata. A submitted outbound message is not treated as recipient delivery unless the saved payload has an explicit delivery status. Seeded or unmatched mail in a saved inbox is marked delivery-unconfirmed, and current pending status comes from final-world membership. Unknown sender and unknown organization are reported separately. `payment` and `inbox_change` interceptions are omitted.

The exporter cannot recover records absent from the saved world and event log. When the final world is missing, event evidence may still produce correspondence rows, but the archive does not claim complete history. The JSON preserves raw saved correspondence records; Markdown shows full bodies and source records.
