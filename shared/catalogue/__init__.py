"""
Getting a business's product catalogue into Krova, from wherever it lives.

Most businesses in this market are not on Shopify. They sell through
WhatsApp and Instagram, or they run their own site - so a catalogue that
only works for Shopify stores would miss almost everyone. Every importer
here lands in the same two tables (Product, ProductVariant) through the
same upsert, so the Demand Board and per-SKU analytics never have to care
where a product came from.
"""
