# The Trust Gate, explained simply

This page explains the Trust Gate in plain language, with no technical background
needed. It covers what it does, how it decides, and (importantly) where our rules
come from, so you can see we are not making the rules up.

## The one-sentence idea

Before any medical data is anonymised and shared, the Trust Gate inspects it and
issues a short report card that says whether the data is good enough to use, and
what it is good enough for.

Think of it like a quality inspection at the door of a factory. Nothing goes down
the line until it has been looked over. The Trust Gate never changes the data; it
only inspects and reports.

## Why this matters

Two reasons.

1. **Bad data leads to bad conclusions.** If a hospital export is missing key
   fields, has impossible values, or uses the wrong codes, any research or
   statistics built on it will be wrong. It is far cheaper to catch that at the
   door than after the analysis.
2. **We anonymise data for a living.** Running broken or mislabelled records
   through anonymisation can quietly produce "clean-looking" records that are
   actually wrong. The Trust Gate stops that before it starts.

## What the Trust Gate actually checks

It asks five plain questions about the data. Each question is a group of automatic
checks.

1. **Is it well-formed?** (we call this *conformance*)
   Does every record have the basic parts it needs: a type, an identity, valid
   dates, and real medical codes rather than blanks or leftovers? Records marked as
   "entered in error" (mistakes the hospital already retracted) are caught here.

2. **Is it complete?** (*completeness*)
   Are the fields that should be filled in actually filled in? A lab result with no
   value, or a patient with no basic details, gets flagged.

3. **Are the values believable?** (*plausibility*)
   Do the numbers make sense? A body temperature of 500, a percentage above 100, or
   a value far outside everything else of its kind gets flagged. We also watch for
   sudden shifts that suggest a measurement unit changed or a device was
   miscalibrated.

4. **Do we know where it came from?** (*provenance*)
   Is there a record of the source system and when it was extracted? This is the
   paper trail that regulated research requires.

5. **Did an independent expert confirm it?** (*external validation*)
   For the strictest uses, we do not just check the data ourselves. We send it to
   independent tools (described below) that confirm the records are correctly formed
   and the medical codes really exist.

## How it decides: a traffic light

After the checks run, the Trust Gate gives one of three answers:

- **Green (PASS)** the data meets every quality bar for its intended use.
- **Amber (CONDITIONAL PASS)** usable, but something fell short. Fix the flagged
  items before high-stakes use.
- **Red (BLOCK)** a serious problem was found. The data is not processed until it
  is fixed.

A single serious defect (for example, records with no identity, or retracted
"entered in error" records) is enough on its own to turn the light red, no matter
how good everything else looks.

## The report card it produces

The result is a short document we call the **Quality Passport**. In plain terms it
contains:

- The traffic-light decision.
- A score and a letter grade (A to F), like a school report, with a separate grade
  for each quality category.
- A clear statement of what the data **is** and **is not** fit for.
- A list of the specific problems found, and how many records were affected.
- An honest note of what could **not** be checked this time.

## "Fit for purpose" is the whole point

Quality is not one number. A car that is perfectly safe for driving around town may
not be safe for a race track. Data works the same way.

The same dataset can be:

- Good enough to **count** how many patients might qualify for a study, but
- Not good enough to draw **medical conclusions** from.

So the passport always states the quality bar *for the specific job you told it you
want to do*, and lists which other jobs the data would and would not pass for. A
lower grade still tells you honestly what the data is safe to use for.

## Where our rules come from (our sources)

This is the important part: we did not invent the quality rules. The Trust Gate is
built on recognised, published standards that hospitals, regulators, and research
networks already use. That is what makes the verdict credible rather than just our
opinion.

| What we build on | What it is, in plain terms | What it gives the Trust Gate |
|---|---|---|
| **HL7 FHIR** | The international standard format for sharing health records. | The common language our data arrives in, so our checks apply anywhere. |
| **The Kahn framework (2016)** | The widely accepted rulebook for judging health-data quality, used by large research networks. | The five questions above, and the definition of "good quality". |
| **OHDSI Data Quality Dashboard** | A proven, published way to score data quality by counting how many records break each rule. | Our scoring method, so scores are comparable to an industry standard. |
| **DAMA and ISO/IEC 25012** | International standards that list the official "dimensions" of data quality (completeness, accuracy, consistency, and so on). | The categories and letter grades on the report card, using names professionals recognise. |
| **Medical code books: LOINC, SNOMED CT, ICD-10, RxNorm, UCUM** | The official dictionaries doctors use for lab tests, clinical terms, diagnoses, medicines, and units. | The reference for checking that codes in the data are real and correctly formed. |
| **Standard statistics (Tukey, Iglewicz-Hoaglin methods)** | Well-known, textbook methods for spotting values that look out of place. | How we flag unbelievable numbers without inventing our own thresholds. |
| **The "fitness for use" principle (Juran; Wang and Strong)** | The classic quality idea that quality means "fit for the job it is needed for". | Why the passport is always tied to a stated purpose, not a single pass/fail. |
| **Safety and governance standards (FHIR Safety Checklist, ISO/HL7 21089, ICH E6 for research)** | Established rules for handling retracted records, provenance, and critical-data focus. | The safety checks and the paper-trail requirements for regulated use. |

## What tools we plug into

Two independent services do the "outside expert" checks, so we are not just marking
our own homework:

- **An independent FHIR validator.** A separate, standards-based tool that confirms
  each record is correctly built. We use the open HL7 and Inferno validator.
- **A medical terminology service.** A live service that confirms a medical code
  (a lab test, a diagnosis, a drug) genuinely exists in the official dictionaries.

If either of these is unavailable, we do not pretend the check passed. We simply
mark it "not checked this time" and say so on the report card (see the promises
below).

## Our four promises

These are the principles that make the Trust Gate trustworthy.

1. **It never changes your data.** It only looks and reports.
2. **It fails safe, never silently.** If a check cannot run, it is recorded as "not
   checked", never counted as a pass. A missing check can never make the data look
   better than it is.
3. **It gives the same answer every time.** The same data in produces the same
   verdict out. The parts that rely on chance (like statistical outlier hints) are
   kept as side notes and never change the traffic light.
4. **It is honest about what it did not check.** If only part of the inspection ran,
   the report says so, so a partial pass is never mistaken for a full clean bill of
   health.

## In one paragraph

The Trust Gate is a quality inspection that runs before we anonymise health data.
It checks whether the data is well-formed, complete, believable, traceable, and
independently confirmed, then gives a traffic-light verdict and a plain report card
that says exactly what the data is fit for. Every rule it uses comes from a
recognised published standard rather than our own invention, it plugs into
independent tools to check the medical details, and it always errs on the side of
honesty: it never edits your data, never hides what it could not check, and gives
the same answer every time.
