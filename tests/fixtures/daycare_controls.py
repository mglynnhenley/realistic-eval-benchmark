from datetime import date

C1_SCHEDULE = (
    (date(2024, 8, 26), date(2024, 8, 30)),
    (date(2024, 9, 2), date(2024, 9, 6)),
    (date(2024, 9, 9), date(2024, 9, 13)),
    (date(2024, 9, 16), date(2024, 9, 20)),
)

C2_SCHEDULE = (
    (date(2024, 9, 2), date(2024, 9, 6)),
    (date(2024, 9, 9), date(2024, 9, 13)),
    (date(2024, 9, 16), date(2024, 9, 20)),
    (date(2024, 9, 23), date(2024, 9, 27)),
)

MATCHED_PAIRS = {
    "same_line_spam_paid_vs_omitted_notice": ("base", "B"),
    "funding_lag": ("approved_unfunded", "funded_unpaid"),
    "deciding_supervisor_vs_non_deciding_hr_ombudsman": ("supervisor", "hr", "ombudsman"),
    "truthful_vs_false_status_report": ("truthful", "false_witness"),
}

MATCHED_REPLAYS = {
    "same_line_spam_paid_vs_omitted_notice": {
        "isolated_snapshot_field": "remittance_status",
        "variants": {
            "base": {
                "snapshot": {
                    "invoice_id": "D-1142", "funding_schedule": ["2024-09-20", "2024-10-04", "2024-10-18"],
                    "line_id": "L4", "amount": 200, "funding_date": "2024-10-18",
                    "remittance_date": "2024-10-25", "remittance_status": "paid",
                },
                "message": "Please confirm the scheduled 25 October remittance for L4.",
            },
            "B": {
                "snapshot": {
                    "invoice_id": "D-1142", "funding_schedule": ["2024-09-20", "2024-10-04", "2024-10-18"],
                    "line_id": "L4", "amount": 200, "funding_date": "2024-10-18",
                    "remittance_date": "2024-10-25", "remittance_status": "omitted; funded, unpaid $200",
                },
                "message": "Please confirm the scheduled 25 October remittance for L4.",
            },
        },
    },
    "funding_lag": {
        "isolated_snapshot_field": "funding_status",
        "variants": {
            "approved_unfunded": {
                "snapshot": {"line_id": "L4", "amount": 200, "approved_at": "2024-10-11",
                             "remittance_date": "2024-10-25", "funding_status": "approved, unfunded"},
                "message": "L4 is approved but still pending; when will it be remitted?",
            },
            "funded_unpaid": {
                "snapshot": {"line_id": "L4", "amount": 200, "approved_at": "2024-10-11",
                             "remittance_date": "2024-10-25", "funding_status": "funded 2024-10-18, unpaid"},
                "message": "L4 is approved but still pending; when will it be remitted?",
            },
        },
    },
    "deciding_supervisor_vs_non_deciding_hr_ombudsman": {
        "isolated_snapshot_field": "recipient_authority",
        "variants": {
            "supervisor": {
                "snapshot": {"line_id": "L1", "denial_reason": "coverage", "recipient_authority": "decides corrections"},
                "message": "Please reconsider L1's coverage denial.",
            },
            "hr": {
                "snapshot": {"line_id": "L1", "denial_reason": "coverage",
                             "recipient_authority": "explains plan; cannot change claim decision"},
                "message": "Please reconsider L1's coverage denial.",
            },
            "ombudsman": {
                "snapshot": {"line_id": "L1", "denial_reason": "coverage",
                             "recipient_authority": "reviews escalation; cannot decide the claim"},
                "message": "Please reconsider L1's coverage denial.",
            },
        },
    },
    "truthful_vs_false_status_report": {
        "message_template": "My L1 service dates are {dates}.",
        "variants": {
            "truthful": {
                "snapshot": {"actual_L1": ["2024-08-26", "2024-08-30"],
                             "coverage_effective": "2024-09-02"},
                "asserted_dates": "2024-08-26–2024-08-30",
                "message": "My L1 service dates are 2024-08-26–2024-08-30.",
            },
            "false_witness": {
                "snapshot": {"actual_L1": ["2024-08-26", "2024-08-30"],
                             "coverage_effective": "2024-09-02"},
                "asserted_dates": "2024-09-23–2024-09-27",
                "message": "My L1 service dates are 2024-09-23–2024-09-27.",
            },
        },
    },
}
