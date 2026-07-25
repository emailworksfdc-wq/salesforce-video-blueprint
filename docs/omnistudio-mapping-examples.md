# OmniStudio Mapping Examples

## Worked Example Rows

| Scenario | Primary intent | Key entities/slots | Actions/orchestration | Guardrails |
| --- | --- | --- | --- | --- |
| Change mailing address | `update_customer_address` | `customerId`, `accountName`, `serviceAddress`, `mailingAddress`, `effectiveDate`, `verificationMethod` | authenticate, fetch profile, validate address, update systems, confirm summary | require strong auth, block disallowed address types, redact sensitive fields, handoff on low confidence |
| Open support case for billing issue | `create_support_case` | `customerId`, `invoiceNumber`, `billingPeriod`, `issueType`, `issueDescription`, `priority`, `contactPreference` | verify identity, classify issue, retrieve invoices, create case, provide case/SLA | no refund promises, redact payment tokens, fraud keyword escalation, duplicate-case suppression |
| Track order status | `track_order_status` | `orderNumber`, `emailOrPhone`, `zipCode`, `orderDate`, `itemName`, `deliveryWindow` | verify ownership, query OMS/carrier, return latest milestone, offer delay fallback | enforce ownership checks, mask tracking/address, fallback when API unavailable, avoid hard delivery guarantees |
