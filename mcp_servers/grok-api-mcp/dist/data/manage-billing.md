# Manage Billing

Handle billing, credits, and payment methods for your xAI API usage.

## Payment Options

### Prepaid Credits

Pre-purchase credits for your team:
- API consumption deducted from available credits
- No overage charges until credits exhausted
- Credits don't expire
- Good for budget control

### Monthly Invoiced Billing

xAI generates a monthly invoice based on your API consumption:
- Used when prepaid credits are not available
- Billed at the end of each billing period
- Detailed usage breakdown included

## Accessing Billing

1. Go to console.x.ai
2. Navigate to "Billing" or "Settings → Billing"
3. Select your team

## Adding Prepaid Credits

### Purchase Credits

1. Go to Billing
2. Click "Add Credits"
3. Select amount
4. Enter payment details
5. Complete purchase

### Credit Packages

Common credit amounts:
- $100
- $500
- $1,000
- $5,000
- Custom amounts available

## Payment Methods

### Supported Methods

- Credit cards (Visa, Mastercard, Amex)
- Debit cards
- Corporate cards
- Wire transfer (Enterprise)

### Adding a Payment Method

1. Go to Billing → Payment Methods
2. Click "Add Payment Method"
3. Enter card details
4. Verify and save

### Removing a Payment Method

1. Go to Payment Methods
2. Find the card to remove
3. Click "Remove"
4. Confirm removal

**Note**: Cannot remove the only payment method if you have active usage.

## Viewing Invoices

### Invoice History

View all past invoices:
- Invoice date
- Amount
- Status (paid/unpaid)
- Download PDF

### Invoice Details

Each invoice includes:
- Billing period
- Usage breakdown by model
- Token counts
- Tool invocations
- Total charges

## Credit Balance

### Viewing Balance

The billing dashboard shows:
- Current credit balance
- Estimated days remaining
- Recent transactions

### Low Balance Alerts

Set up alerts when credits are low:
1. Go to Billing Settings
2. Set threshold amount
3. Choose notification method
4. Save settings

## Cost Breakdown

### By Model

| Model | Input/1M | Output/1M |
|-------|----------|-----------|
| grok-4 | $2.00 | $10.00 |
| grok-4-fast | $0.20 | $0.50 |
| grok-4-1-fast-non-reasoning | $0.10 | $0.25 |

### By Tool

| Tool | Cost |
|------|------|
| Web Search | $0.025/source |
| Document Search | $10/1,000 invocations |
| Code Execution | Included |

## Budget Controls

### Setting Budgets

1. Go to Billing → Budgets
2. Set monthly budget limit
3. Configure actions when limit reached:
   - Alert only
   - Throttle usage
   - Block usage

### Per-Key Budgets

Assign budgets to specific API keys:
- Test key: $100/month
- Production key: $5,000/month

## Billing Contacts

### Primary Contact

Receives all billing communications:
- Invoices
- Payment reminders
- Budget alerts

### Additional Contacts

Add more recipients for billing emails:
1. Go to Billing Contacts
2. Click "Add Contact"
3. Enter email
4. Select notification types

## Tax Information

### Adding Tax ID

For businesses requiring tax documentation:
1. Go to Billing → Tax Info
2. Enter tax ID/VAT number
3. Provide business address
4. Save information

### Tax Exemption

Contact support@x.ai for tax exemption requests.

## Enterprise Billing

For Enterprise customers:
- Custom contracts
- Volume discounts
- Net payment terms
- Dedicated billing support
- Custom invoicing

Contact sales for Enterprise billing options.

## Troubleshooting

### Payment Failed

- Check card details
- Verify sufficient funds
- Contact your bank
- Try alternative payment method

### Invoice Dispute

Contact support@x.ai with:
- Invoice number
- Specific charges in question
- Supporting information
