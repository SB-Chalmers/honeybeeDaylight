"""Frontend energy selections mapped to standard Honeybee Energy identifiers."""

PROGRAM_IDENTIFIERS = {
    'Office': 'Generic Office Program',
    'Residential': 'Generic Apartment',
    'Retail': 'Generic Retail',
    'School': 'Generic Classroom',
    'Hospital': 'Generic Hospital',
}

HVAC_IDENTIFIERS = {
    'Default HVAC': 'IdealAirSystem',
    'VAV': 'VAVwithPFP',
    'CAV': 'AllAirSystem',
    'Radiant': 'RadiantConvective',
    'Split System': 'PTAC',
}


def program_identifier(name: str) -> str:
    return PROGRAM_IDENTIFIERS.get(name, PROGRAM_IDENTIFIERS['Office'])


def hvac_identifier(name: str) -> str:
    return HVAC_IDENTIFIERS.get(name, HVAC_IDENTIFIERS['Default HVAC'])