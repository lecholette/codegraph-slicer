"""Tiny budget guard used by the README and HELP flow examples."""


def lookup_rate(gpu):
    return {"a40": 49, "h100": 299}.get(gpu, 100)


def admit(jobs, billed_cents, cap_cents, gpu, hours):
    liability = 0
    for job in jobs:
        if job.open:
            liability += job.reserved_cents
    new_estimate_cents = lookup_rate(gpu) * hours
    total = billed_cents + liability + new_estimate_cents
    if total > cap_cents:
        return False
    return True
