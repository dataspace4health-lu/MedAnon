/** Common LOINC codes → human-readable label. Used to replace raw code strings in the UI. */
export const LOINC_LABELS: Record<string, string> = {
  // Vitals
  "8302-2":  "Body height",
  "29463-7": "Body weight",
  "39156-5": "BMI",
  "8867-4":  "Heart rate",
  "9279-1":  "Respiratory rate",
  "8310-5":  "Body temperature",
  "59408-5": "Oxygen saturation",
  "8480-6":  "Systolic BP",
  "8462-4":  "Diastolic BP",
  "55284-4": "Blood pressure",
  // Metabolic / lipids
  "2093-3":  "Total cholesterol",
  "2571-8":  "Triglycerides",
  "18262-6": "LDL cholesterol",
  "2085-9":  "HDL cholesterol",
  "2339-0":  "Glucose",
  "4548-4":  "Hemoglobin A1c",
  // Renal / hepatic
  "38483-4": "Creatinine",
  "2160-0":  "Creatinine (serum)",
  "33914-3": "eGFR",
  "1742-6":  "ALT",
  "1920-8":  "AST",
  "17861-6": "Calcium",
  // Haematology, CBC
  "718-7":   "Hemoglobin",
  "4544-3":  "Hematocrit",
  "6690-2":  "WBC count",
  "777-3":   "Platelet count",
  "789-8":   "RBC count",
  "787-2":   "MCV (mean corpuscular volume)",
  "785-6":   "MCH (mean corpuscular hemoglobin)",
  "786-4":   "MCHC",
  "788-0":   "RDW (red cell distribution width)",
  "32207-3": "Platelet distribution width",
  "32623-1": "Mean platelet volume (MPV)",
  // CBC differential
  "770-8":   "Neutrophils %",
  "736-9":   "Lymphocytes %",
  "5905-5":  "Monocytes %",
  "713-8":   "Eosinophils %",
  "706-2":   "Basophils %",
  "751-8":   "Neutrophils (absolute)",
  "731-0":   "Lymphocytes (absolute)",
  "742-7":   "Monocytes (absolute)",
  "711-2":   "Eosinophils (absolute)",
  "704-7":   "Basophils (absolute)",
  // Basic / comprehensive metabolic panel
  "2951-2":  "Sodium",
  "2823-3":  "Potassium",
  "2075-0":  "Chloride",
  "2028-9":  "CO2 (bicarbonate)",
  "3094-0":  "BUN (urea nitrogen)",
  "6299-2":  "BUN (blood)",
  "2345-7":  "Glucose (serum)",
  "2885-2":  "Total protein",
  "1751-7":  "Albumin",
  "1975-2":  "Total bilirubin",
  "6768-6":  "Alkaline phosphatase (ALP)",
  "49765-1": "Calcium (serum)",
  // Electrolytes / blood gas (LOINC blood-specimen variants)
  "6298-4":  "Potassium (blood)",
  "20565-8": "CO2 total (blood)",
  // Paediatric / perinatal
  "74006-8": "Delivery weight",
  // Other common
  "72514-3": "Pain severity (0–10)",
  "72166-2": "Tobacco use",
  "8661-1":  "Gestational age",
  "56832-9": "Smoking status",

};

/** True when we have a human label for this LOINC code (else it's a raw code). */
export function hasLoincLabel(code: string): boolean {
  return code in LOINC_LABELS;
}

/** Return a human label for a LOINC code, falling back to the raw code. */
export function loincLabel(code: string): string {
  return LOINC_LABELS[code] ?? code;
}

/** Shorten a code-system URI to its common abbreviation. */
export function codeSystemLabel(uri: string): string {
  if (uri.includes("loinc.org"))       return "LOINC";
  if (uri.includes("snomed"))          return "SNOMED CT";
  // EU code systems (must check icd-10-gm before the generic icd-10).
  if (uri.includes("icd-10-gm"))       return "ICD-10-GM";
  if (uri.includes("icd-10"))          return "ICD-10";
  if (uri.includes("rxnorm"))          return "RxNorm";
  if (uri.includes("whocc.no/atc"))    return "ATC";
  if (uri.includes("edqm"))            return "EDQM";
  if (uri.includes("cvx"))             return "CVX";
  if (uri.includes("unitsofmeasure"))  return "UCUM";
  if (uri.includes("observation-category")) return "Obs-Category";
  if (uri.includes("condition-category"))   return "Cond-Category";
  if (uri.includes("encounter-type"))       return "Encounter-Type";
  // Strip leading http(s):// and trailing slash
  return uri.replace(/^https?:\/\//, "").replace(/\/$/, "");
}
