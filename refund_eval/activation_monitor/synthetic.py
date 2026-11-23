"""Agent-authored synthetic matched claim/evidence examples."""

import json
from pathlib import Path

TRAIN_SCENARIOS = (
    ("The North Annex register records inspection 14 as closed on 14 March.",
     "Inspection 14 closure is dated 14 March in the North Annex register.",
     "The North Annex log dates inspection 14 closure to 18 March.",
     "The register gives the inspector and room number for inspection 14; the closure date is not listed."),
    ("Account Kestrel's authorization expires on 30 September.",
     "The Kestrel authorization entry ends on 30 September.",
     "The account sheet sets Kestrel's expiry at 31 October.",
     "The Kestrel file shows its account tier and owner, with no authorization expiry."),
    ("Parcel R41 weighs 6.2 kilograms.",
     "Scale record R41: 6.2 kg.",
     "The dispatch sheet records 7.0 kg for R41.",
     "The parcel manifest gives R41's destination and carrier only."),
    ("Mira's appointment was moved to Monday.",
     "A revised calendar places Mira's appointment on Monday.",
     "Mira's calendar keeps the appointment on Thursday.",
     "The appointment note lists the clinic and room, without an updated day."),
    ("Panel 7 in the mural inventory is attributed to Sol Ibarra.",
     "The catalogue credits Sol Ibarra for mural panel 7.",
     "Panel 7's maker is recorded as Nia Cor.",
     "The inventory entry for panel 7 gives its material and dimensions, not its artist."),
    ("Workshop 18 begins at 08:40.",
     "The workshop roster schedules number 18 for 8:40 a.m.",
     "The timetable sets workshop 18 to start at 09:10.",
     "Workshop 18's room and facilitator appear in the roster; start time is unfilled."),
    ("Ledger entry 92 records a deposit of £73.",
     "Entry 92 shows a deposit for seventy-three pounds.",
     "The ledger line for 92 is a £37 deposit.",
     "Entry 92 has a payee and reference code, but no amount is included."),
    ("The botanical sample was collected at Ridge Site.",
     "The collection sheet places the specimen at Ridge Site.",
     "Sampling coordinates locate the specimen at Valley Plot.",
     "The sample record notes its collector and date; its site field is blank."),
    ("The bronze key belongs to cabinet C.",
     "Cabinet C is paired with the bronze key in the key register.",
     "The register pairs that bronze key with cabinet F.",
     "The key inventory describes the key's color and cut but does not state a cabinet."),
    ("The third audit occurred in 2022.",
     "Audit number three is dated 2022 in the archive.",
     "The audit chronology places the third review in 2021.",
     "The audit folder identifies its reviewer and scope; a year is not recorded."),
    ("The Echo Ferry departs from Pier South.",
     "The ferry schedule lists Pier South as Echo's departure pier.",
     "Echo's timetable names North Quay as its departure point.",
     "The route sheet lists the Echo Ferry's stops but omits its starting pier."),
    ("The blue folder is stored on shelf 4.",
     "The storage index places the blue folder on shelf four.",
     "The archive map locates the blue folder on shelf 9.",
     "The inventory records the blue folder's title and owner, not its shelf."),
)

EVAL_SCENARIOS = (
    ("On the Oriole chart, marker 3 denotes the winter route.",
     "Marker three maps to the winter track in the Oriole legend.",
     "Oriole's key assigns marker 3 to the summer route.",
     "The Oriole sheet explains marker shapes and colors; no season is assigned to marker 3."),
    ("Aster kiln batch 6 reached 840 degrees Celsius.",
     "Thermograph for batch six peaks at 840°C.",
     "The kiln log records a maximum of 760°C for batch six.",
     "Batch six's firing duration is logged, but no temperature reading appears."),
    ("The Fallow Creek survey counted 28 nesting pairs.",
     "Twenty-eight nesting pairs appear in the Fallow Creek count.",
     "Survey tally at Fallow Creek lists 19 nesting pairs.",
     "The creek report maps the survey zones and dates; it gives no pair count."),
    ("Invoice Vela-204 was settled on 3 August.",
     "Payment history marks Vela-204 paid on 3 August.",
     "The remittance record shows Vela-204 cleared on 5 August.",
     "The invoice archive includes a currency code and vendor address; settlement date is absent."),
    ("The Lumen device uses firmware version 2.7.",
     "Lumen's configuration page identifies firmware release 2.7.",
     "The device log reports firmware 3.1.",
     "The setup record lists Lumen's serial and warranty; software version is not shown."),
    ("North Hall's west exit is 12 meters from the stage.",
     "A floor plan measures the west exit at twelve meters from the stage.",
     "The west exit is dimensioned at 18 meters from the stage.",
     "The hall plan labels exits and stage dimensions separately, with no west-exit distance."),
    ("The amber vial contains 4 milliliters of buffer.",
     "The lab note records four mL in the amber buffer vial.",
     "The vial register lists 2.5 mL for that amber container.",
     "The vial card identifies buffer type and batch; fill volume is missing."),
    ("The Juniper bus reaches East Gate at 17:25.",
     "The Juniper timetable gives East Gate an arrival of 17:25.",
     "Its schedule lists East Gate arrival at 17:50.",
     "The bus route document lists East Gate as a stop, but no arrival time."),
)


def write_synthetic_examples(path: str | Path) -> int:
    """Write 12 train and 8 held-out, scenario-unique matched groups (40 rows)."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for split, scenarios in (("train", TRAIN_SCENARIOS), ("eval", EVAL_SCENARIOS)):
        for index, (claim, supported, contradicted, absent) in enumerate(scenarios):
            group_id = f"{split}-scenario-{index + 1:02d}"
            unsupported = contradicted if index % 2 == 0 else absent
            for label, evidence in (("supported", supported), ("unsupported", unsupported)):
                rows.append({
                    "id": f"{split[:2]}-{index + 1:02d}-{label[:3]}",
                    "trial_id": f"agent-authored-synthetic-{split}-trial",
                    "group_id": group_id,
                    "split": split,
                    "evidence_context": evidence,
                    "claim": claim,
                    "label": label,
                    "label_provenance": "agent-authored synthetic fixture",
                })
    destination.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return len(rows)
