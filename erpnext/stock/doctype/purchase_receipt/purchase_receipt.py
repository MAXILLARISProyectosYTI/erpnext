# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt


import frappe
from frappe import _, throw
from frappe.desk.notifications import clear_doctype_notifications
from frappe.model.mapper import get_mapped_doc
from frappe.query_builder.functions import CombineDatetime
from frappe.utils import cint, flt, getdate, nowdate
from pypika import functions as fn

import erpnext
from erpnext.accounts.utils import get_account_currency
from erpnext.assets.doctype.asset.asset import get_asset_account, is_cwip_accounting_enabled
from erpnext.assets.doctype.asset_category.asset_category import get_asset_category_account
from erpnext.buying.utils import check_on_hold_or_closed_status
from erpnext.controllers.buying_controller import BuyingController
from erpnext.stock.doctype.delivery_note.delivery_note import make_inter_company_transaction

form_grid_templates = {"items": "templates/form_grid/item_grid.html"}


class PurchaseReceipt(BuyingController):
	def __init__(self, *args, **kwargs):
		super(PurchaseReceipt, self).__init__(*args, **kwargs)
		self.status_updater = [
			{
				"target_dt": "Purchase Order Item",
				"join_field": "purchase_order_item",
				"target_field": "received_qty",
				"target_parent_dt": "Purchase Order",
				"target_parent_field": "per_received",
				"target_ref_field": "qty",
				"source_dt": "Purchase Receipt Item",
				"source_field": "received_qty",
				"second_source_dt": "Purchase Invoice Item",
				"second_source_field": "received_qty",
				"second_join_field": "po_detail",
				"percent_join_field": "purchase_order",
				"overflow_type": "receipt",
				"second_source_extra_cond": """ and exists(select name from `tabPurchase Invoice`
				where name=`tabPurchase Invoice Item`.parent and update_stock = 1)""",
			},
			{
				"source_dt": "Purchase Receipt Item",
				"target_dt": "Material Request Item",
				"join_field": "material_request_item",
				"target_field": "received_qty",
				"target_parent_dt": "Material Request",
				"target_parent_field": "per_received",
				"target_ref_field": "stock_qty",
				"source_field": "stock_qty",
				"percent_join_field": "material_request",
			},
			{
				"source_dt": "Purchase Receipt Item",
				"target_dt": "Purchase Invoice Item",
				"join_field": "purchase_invoice_item",
				"target_field": "received_qty",
				"target_parent_dt": "Purchase Invoice",
				"target_parent_field": "per_received",
				"target_ref_field": "qty",
				"source_field": "received_qty",
				"percent_join_field": "purchase_invoice",
				"overflow_type": "receipt",
			},
			{
				"source_dt": "Purchase Receipt Item",
				"target_dt": "Delivery Note Item",
				"join_field": "delivery_note_item",
				"source_field": "received_qty",
				"target_field": "received_qty",
				"target_parent_dt": "Delivery Note",
				"target_ref_field": "qty",
				"overflow_type": "receipt",
			},
		]

		if cint(self.is_return):
			self.status_updater.extend(
				[
					{
						"source_dt": "Purchase Receipt Item",
						"target_dt": "Purchase Order Item",
						"join_field": "purchase_order_item",
						"target_field": "returned_qty",
						"source_field": "-1 * qty",
						"second_source_dt": "Purchase Invoice Item",
						"second_source_field": "-1 * qty",
						"second_join_field": "po_detail",
						"extra_cond": """ and exists (select name from `tabPurchase Receipt`
						where name=`tabPurchase Receipt Item`.parent and is_return=1)""",
						"second_source_extra_cond": """ and exists (select name from `tabPurchase Invoice`
						where name=`tabPurchase Invoice Item`.parent and is_return=1 and update_stock=1)""",
					},
					{
						"source_dt": "Purchase Receipt Item",
						"target_dt": "Purchase Receipt Item",
						"join_field": "purchase_receipt_item",
						"target_field": "returned_qty",
						"target_parent_dt": "Purchase Receipt",
						"target_parent_field": "per_returned",
						"target_ref_field": "received_stock_qty",
						"source_field": "-1 * received_stock_qty",
						"percent_join_field_parent": "return_against",
					},
				]
			)

	def before_validate(self):
		from erpnext.stock.doctype.putaway_rule.putaway_rule import apply_putaway_rule

		if self.get("items") and self.apply_putaway_rule and not self.get("is_return"):
			apply_putaway_rule(self.doctype, self.get("items"), self.company)

	def validate(self):
		self.validate_posting_time()
		super(PurchaseReceipt, self).validate()

		if self._action != "submit":
			self.set_status()

		self.po_required()
		self.validate_items_quality_inspection()
		self.validate_with_previous_doc()
		self.validate_uom_is_integer("uom", ["qty", "received_qty"])
		self.validate_uom_is_integer("stock_uom", "stock_qty")
		self.validate_cwip_accounts()
		self.validate_provisional_expense_account()

		self.check_on_hold_or_closed_status()

		if getdate(self.posting_date) > getdate(nowdate()):
			throw(_("Posting Date cannot be future date"))

		self.get_current_stock()
		self.reset_default_field_value("set_warehouse", "items", "warehouse")
		self.reset_default_field_value("rejected_warehouse", "items", "rejected_warehouse")
		self.reset_default_field_value("set_from_warehouse", "items", "from_warehouse")

	def validate_cwip_accounts(self):
		for item in self.get("items"):
			if item.is_fixed_asset and is_cwip_accounting_enabled(item.asset_category):
				# check cwip accounts before making auto assets
				# Improves UX by not giving messages of "Assets Created" before throwing error of not finding arbnb account
				arbnb_account = self.get_company_default("asset_received_but_not_billed")
				cwip_account = get_asset_account(
					"capital_work_in_progress_account", asset_category=item.asset_category, company=self.company
				)
				break

	def validate_provisional_expense_account(self):
		provisional_accounting_for_non_stock_items = cint(
			frappe.db.get_value(
				"Company", self.company, "enable_provisional_accounting_for_non_stock_items"
			)
		)

		if not provisional_accounting_for_non_stock_items:
			return

		default_provisional_account = self.get_company_default("default_provisional_account")
		for item in self.get("items"):
			if not item.get("provisional_expense_account"):
				item.provisional_expense_account = default_provisional_account

	def validate_with_previous_doc(self):
		super(PurchaseReceipt, self).validate_with_previous_doc(
			{
				"Purchase Order": {
					"ref_dn_field": "purchase_order",
					"compare_fields": [["supplier", "="], ["company", "="], ["currency", "="]],
				},
				"Purchase Order Item": {
					"ref_dn_field": "purchase_order_item",
					"compare_fields": [["project", "="], ["uom", "="], ["item_code", "="]],
					"is_child_table": True,
					"allow_duplicate_prev_row_id": True,
				},
			}
		)

		if (
			cint(frappe.db.get_single_value("Buying Settings", "maintain_same_rate"))
			and not self.is_return
			and not self.is_internal_supplier
		):
			self.validate_rate_with_reference_doc(
				[["Purchase Order", "purchase_order", "purchase_order_item"]]
			)

	def po_required(self):
		if frappe.db.get_value("Buying Settings", None, "po_required") == "Yes":
			for d in self.get("items"):
				if not d.purchase_order:
					frappe.throw(_("Purchase Order number required for Item {0}").format(d.item_code))

	def validate_items_quality_inspection(self):
		for item in self.get("items"):
			if item.quality_inspection:
				qi = frappe.db.get_value(
					"Quality Inspection",
					item.quality_inspection,
					["reference_type", "reference_name", "item_code"],
					as_dict=True,
				)

				if qi.reference_type != self.doctype or qi.reference_name != self.name:
					msg = f"""Row #{item.idx}: Please select a valid Quality Inspection with Reference Type
						{frappe.bold(self.doctype)} and Reference Name {frappe.bold(self.name)}."""
					frappe.throw(_(msg))

				if qi.item_code != item.item_code:
					msg = f"""Row #{item.idx}: Please select a valid Quality Inspection with Item Code
						{frappe.bold(item.item_code)}."""
					frappe.throw(_(msg))

	def get_already_received_qty(self, po, po_detail):
		qty = frappe.db.sql(
			"""select sum(qty) from `tabPurchase Receipt Item`
			where purchase_order_item = %s and docstatus = 1
			and purchase_order=%s
			and parent != %s""",
			(po_detail, po, self.name),
		)
		return qty and flt(qty[0][0]) or 0.0

	def get_po_qty_and_warehouse(self, po_detail):
		po_qty, po_warehouse = frappe.db.get_value(
			"Purchase Order Item", po_detail, ["qty", "warehouse"]
		)
		return po_qty, po_warehouse

	# Check for Closed status
	def check_on_hold_or_closed_status(self):
		check_list = []
		for d in self.get("items"):
			if (
				d.meta.get_field("purchase_order") and d.purchase_order and d.purchase_order not in check_list
			):
				check_list.append(d.purchase_order)
				check_on_hold_or_closed_status("Purchase Order", d.purchase_order)

	# on submit
	def on_submit(self):
		super(PurchaseReceipt, self).on_submit()

		# Check for Approving Authority
		frappe.get_doc("Authorization Control").validate_approving_authority(
			self.doctype, self.company, self.base_grand_total
		)

		self.update_prevdoc_status()
		if flt(self.per_billed) < 100:
			self.update_billing_status()
		else:
			self.db_set("status", "Completed")

		# Updating stock ledger should always be called after updating prevdoc status,
		# because updating ordered qty, reserved_qty_for_subcontract in bin
		# depends upon updated ordered qty in PO
		self.update_stock_ledger()
		self.make_gl_entries()
		self.repost_future_sle_and_gle()
		self.set_consumed_qty_in_subcontract_order()

	def check_next_docstatus(self):
		submit_rv = frappe.db.sql(
			"""select t1.name
			from `tabPurchase Invoice` t1,`tabPurchase Invoice Item` t2
			where t1.name = t2.parent and t2.purchase_receipt = %s and t1.docstatus = 1""",
			(self.name),
		)
		if submit_rv:
			frappe.throw(_("Purchase Invoice {0} is already submitted").format(self.submit_rv[0][0]))

	def on_cancel(self):
		super(PurchaseReceipt, self).on_cancel()

		self.check_on_hold_or_closed_status()
		# Check if Purchase Invoice has been submitted against current Purchase Order
		submitted = frappe.db.sql(
			"""select t1.name
			from `tabPurchase Invoice` t1,`tabPurchase Invoice Item` t2
			where t1.name = t2.parent and t2.purchase_receipt = %s and t1.docstatus = 1""",
			self.name,
		)
		if submitted:
			frappe.throw(_("Purchase Invoice {0} is already submitted").format(submitted[0][0]))

		self.update_prevdoc_status()
		self.update_billing_status()

		# Updating stock ledger should always be called after updating prevdoc status,
		# because updating ordered qty in bin depends upon updated ordered qty in PO
		self.update_stock_ledger()
		self.make_gl_entries_on_cancel()
		self.repost_future_sle_and_gle()
		self.ignore_linked_doctypes = (
			"GL Entry",
			"Stock Ledger Entry",
			"Repost Item Valuation",
			"Serial and Batch Bundle",
		)
		self.delete_auto_created_batches()
		self.set_consumed_qty_in_subcontract_order()

	def get_gl_entries(self, warehouse_account=None):
		from erpnext.accounts.general_ledger import process_gl_map

		gl_entries = []

		self.make_item_gl_entries(gl_entries, warehouse_account=warehouse_account)
		self.make_tax_gl_entries(gl_entries)
		self.get_asset_gl_entry(gl_entries)

		return process_gl_map(gl_entries)

	def make_item_gl_entries(self, gl_entries, warehouse_account=None):
		from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import (
			get_purchase_document_details,
		)

		stock_rbnb = None
		if erpnext.is_perpetual_inventory_enabled(self.company):
			stock_rbnb = self.get_company_default("stock_received_but_not_billed")
			landed_cost_entries = get_item_account_wise_additional_cost(self.name)
			expenses_included_in_valuation = self.get_company_default("expenses_included_in_valuation")

		warehouse_with_no_account = []
		stock_items = self.get_stock_items()
		provisional_accounting_for_non_stock_items = cint(
			frappe.db.get_value(
				"Company", self.company, "enable_provisional_accounting_for_non_stock_items"
			)
		)

		exchange_rate_map, net_rate_map = get_purchase_document_details(self)

		for d in self.get("items"):
			if d.item_code in stock_items and flt(d.valuation_rate) and flt(d.qty):
				if warehouse_account.get(d.warehouse):
					stock_value_diff = frappe.db.get_value(
						"Stock Ledger Entry",
						{
							"voucher_type": "Purchase Receipt",
							"voucher_no": self.name,
							"voucher_detail_no": d.name,
							"warehouse": d.warehouse,
							"is_cancelled": 0,
						},
						"stock_value_difference",
					)

					warehouse_account_name = warehouse_account[d.warehouse]["account"]
					warehouse_account_currency = warehouse_account[d.warehouse]["account_currency"]
					supplier_warehouse_account = warehouse_account.get(self.supplier_warehouse, {}).get("account")
					supplier_warehouse_account_currency = warehouse_account.get(self.supplier_warehouse, {}).get(
						"account_currency"
					)
					remarks = self.get("remarks") or _("Accounting Entry for Stock")

					# If PR is sub-contracted and fg item rate is zero
					# in that case if account for source and target warehouse are same,
					# then GL entries should not be posted
					if (
						flt(stock_value_diff) == flt(d.rm_supp_cost)
						and warehouse_account.get(self.supplier_warehouse)
						and warehouse_account_name == supplier_warehouse_account
					):
						continue

					self.add_gl_entry(
						gl_entries=gl_entries,
						account=warehouse_account_name,
						cost_center=d.cost_center,
						debit=stock_value_diff,
						credit=0.0,
						remarks=remarks,
						against_account=stock_rbnb,
						account_currency=warehouse_account_currency,
						item=d,
					)

					# GL Entry for from warehouse or Stock Received but not billed
					# Intentionally passed negative debit amount to avoid incorrect GL Entry validation
					credit_currency = (
						get_account_currency(warehouse_account[d.from_warehouse]["account"])
						if d.from_warehouse
						else get_account_currency(stock_rbnb)
					)

					credit_amount = (
						flt(d.base_net_amount, d.precision("base_net_amount"))
						if credit_currency == self.company_currency
						else flt(d.net_amount, d.precision("net_amount"))
					)

					outgoing_amount = d.base_net_amount
					if self.is_internal_transfer() and d.valuation_rate:
						outgoing_amount = abs(
							frappe.db.get_value(
								"Stock Ledger Entry",
								{
									"voucher_type": "Purchase Receipt",
									"voucher_no": self.name,
									"voucher_detail_no": d.name,
									"warehouse": d.from_warehouse,
									"is_cancelled": 0,
								},
								"stock_value_difference",
							)
						)
						credit_amount = outgoing_amount

					if credit_amount:
						account = warehouse_account[d.from_warehouse]["account"] if d.from_warehouse else stock_rbnb

						self.add_gl_entry(
							gl_entries=gl_entries,
							account=account,
							cost_center=d.cost_center,
							debit=-1 * flt(outgoing_amount, d.precision("base_net_amount")),
							credit=0.0,
							remarks=remarks,
							against_account=warehouse_account_name,
							debit_in_account_currency=-1 * credit_amount,
							account_currency=credit_currency,
							item=d,
						)

						# check if the exchange rate has changed
						if d.get("purchase_invoice"):
							if (
								exchange_rate_map[d.purchase_invoice]
								and self.conversion_rate != exchange_rate_map[d.purchase_invoice]
								and d.net_rate == net_rate_map[d.purchase_invoice_item]
							):

								discrepancy_caused_by_exchange_rate_difference = (d.qty * d.net_rate) * (
									exchange_rate_map[d.purchase_invoice] - self.conversion_rate
								)

								self.add_gl_entry(
									gl_entries=gl_entries,
									account=account,
									cost_center=d.cost_center,
									debit=0.0,
									credit=discrepancy_caused_by_exchange_rate_difference,
									remarks=remarks,
									against_account=self.supplier,
									debit_in_account_currency=-1 * discrepancy_caused_by_exchange_rate_difference,
									account_currency=credit_currency,
									item=d,
								)

								self.add_gl_entry(
									gl_entries=gl_entries,
									account=self.get_company_default("exchange_gain_loss_account"),
									cost_center=d.cost_center,
									debit=discrepancy_caused_by_exchange_rate_difference,
									credit=0.0,
									remarks=remarks,
									against_account=self.supplier,
									debit_in_account_currency=-1 * discrepancy_caused_by_exchange_rate_difference,
									account_currency=credit_currency,
									item=d,
								)

					# Amount added through landed-cos-voucher
					if d.landed_cost_voucher_amount and landed_cost_entries:
						if (d.item_code, d.name) in landed_cost_entries:
							for account, amount in landed_cost_entries[(d.item_code, d.name)].items():
								account_currency = get_account_currency(account)
								credit_amount = (
									flt(amount["base_amount"])
									if (amount["base_amount"] or account_currency != self.company_currency)
									else flt(amount["amount"])
								)

								self.add_gl_entry(
									gl_entries=gl_entries,
									account=account,
									cost_center=d.cost_center,
									debit=0.0,
									credit=credit_amount,
									remarks=remarks,
									against_account=warehouse_account_name,
									credit_in_account_currency=flt(amount["amount"]),
									account_currency=account_currency,
									project=d.project,
									item=d,
								)

					if d.rate_difference_with_purchase_invoice and stock_rbnb:
						account_currency = get_account_currency(stock_rbnb)
						self.add_gl_entry(
							gl_entries=gl_entries,
							account=stock_rbnb,
							cost_center=d.cost_center,
							debit=0.0,
							credit=flt(d.rate_difference_with_purchase_invoice),
							remarks=_("Adjustment based on Purchase Invoice rate"),
							against_account=warehouse_account_name,
							account_currency=account_currency,
							project=d.project,
							item=d,
						)

					# sub-contracting warehouse
					if flt(d.rm_supp_cost) and warehouse_account.get(self.supplier_warehouse):
						self.add_gl_entry(
							gl_entries=gl_entries,
							account=supplier_warehouse_account,
							cost_center=d.cost_center,
							debit=0.0,
							credit=flt(d.rm_supp_cost),
							remarks=remarks,
							against_account=warehouse_account_name,
							account_currency=supplier_warehouse_account_currency,
							item=d,
						)

					# divisional loss adjustment
					valuation_amount_as_per_doc = (
						flt(outgoing_amount, d.precision("base_net_amount"))
						+ flt(d.landed_cost_voucher_amount)
						+ flt(d.rm_supp_cost)
						+ flt(d.item_tax_amount)
						+ flt(d.rate_difference_with_purchase_invoice)
					)

					divisional_loss = flt(
						valuation_amount_as_per_doc - flt(stock_value_diff), d.precision("base_net_amount")
					)

					if divisional_loss:
						if self.is_return or flt(d.item_tax_amount):
							loss_account = expenses_included_in_valuation
						else:
							loss_account = (
								self.get_company_default("default_expense_account", ignore_validation=True) or stock_rbnb
							)

						cost_center = d.cost_center or frappe.get_cached_value(
							"Company", self.company, "cost_center"
						)

						self.add_gl_entry(
							gl_entries=gl_entries,
							account=loss_account,
							cost_center=cost_center,
							debit=divisional_loss,
							credit=0.0,
							remarks=remarks,
							against_account=warehouse_account_name,
							account_currency=credit_currency,
							project=d.project,
							item=d,
						)

				elif (
					d.warehouse not in warehouse_with_no_account
					or d.rejected_warehouse not in warehouse_with_no_account
				):
					warehouse_with_no_account.append(d.warehouse)
			elif (
				d.item_code not in stock_items
				and not d.is_fixed_asset
				and flt(d.qty)
				and provisional_accounting_for_non_stock_items
				and d.get("provisional_expense_account")
			):
				self.add_provisional_gl_entry(
					d, gl_entries, self.posting_date, d.get("provisional_expense_account")
				)

		if warehouse_with_no_account:
			frappe.msgprint(
				_("No accounting entries for the following warehouses")
				+ ": \n"
				+ "\n".join(warehouse_with_no_account)
			)

	def add_provisional_gl_entry(
		self, item, gl_entries, posting_date, provisional_account, reverse=0
	):
		credit_currency = get_account_currency(provisional_account)
		debit_currency = get_account_currency(item.expense_account)
		expense_account = item.expense_account
		remarks = self.get("remarks") or _("Accounting Entry for Service")
		multiplication_factor = 1

		if reverse:
			multiplication_factor = -1
			expense_account = frappe.db.get_value(
				"Purchase Receipt Item", {"name": item.get("pr_detail")}, ["expense_account"]
			)

		self.add_gl_entry(
			gl_entries=gl_entries,
			account=provisional_account,
			cost_center=item.cost_center,
			debit=0.0,
			credit=multiplication_factor * item.amount,
			remarks=remarks,
			against_account=expense_account,
			account_currency=credit_currency,
			project=item.project,
			voucher_detail_no=item.name,
			item=item,
			posting_date=posting_date,
		)

		self.add_gl_entry(
			gl_entries=gl_entries,
			account=expense_account,
			cost_center=item.cost_center,
			debit=multiplication_factor * item.amount,
			credit=0.0,
			remarks=remarks,
			against_account=provisional_account,
			account_currency=debit_currency,
			project=item.project,
			voucher_detail_no=item.name,
			item=item,
			posting_date=posting_date,
		)

	def make_tax_gl_entries(self, gl_entries):

		if erpnext.is_perpetual_inventory_enabled(self.company):
			expenses_included_in_valuation = self.get_company_default("expenses_included_in_valuation")

		negative_expense_to_be_booked = sum([flt(d.item_tax_amount) for d in self.get("items")])
		# Cost center-wise amount breakup for other charges included for valuation
		valuation_tax = {}
		for tax in self.get("taxes"):
			if tax.category in ("Valuation", "Valuation and Total") and flt(
				tax.base_tax_amount_after_discount_amount
			):
				if not tax.cost_center:
					frappe.throw(
						_("Cost Center is required in row {0} in Taxes table for type {1}").format(
							tax.idx, _(tax.category)
						)
					)
				valuation_tax.setdefault(tax.name, 0)
				valuation_tax[tax.name] += (tax.add_deduct_tax == "Add" and 1 or -1) * flt(
					tax.base_tax_amount_after_discount_amount
				)

		if negative_expense_to_be_booked and valuation_tax:
			# Backward compatibility:
			# If expenses_included_in_valuation account has been credited in against PI
			# and charges added via Landed Cost Voucher,
			# post valuation related charges on "Stock Received But Not Billed"
			# introduced in 2014 for backward compatibility of expenses already booked in expenses_included_in_valuation account

			negative_expense_booked_in_pi = frappe.db.sql(
				"""select name from `tabPurchase Invoice Item` pi
				where docstatus = 1 and purchase_receipt=%s
				and exists(select name from `tabGL Entry` where voucher_type='Purchase Invoice'
					and voucher_no=pi.parent and account=%s)""",
				(self.name, expenses_included_in_valuation),
			)

			against_account = ", ".join([d.account for d in gl_entries if flt(d.debit) > 0])
			total_valuation_amount = sum(valuation_tax.values())
			amount_including_divisional_loss = negative_expense_to_be_booked
			stock_rbnb = self.get_company_default("stock_received_but_not_billed")
			i = 1
			for tax in self.get("taxes"):
				if valuation_tax.get(tax.name):

					if negative_expense_booked_in_pi:
						account = stock_rbnb
					else:
						account = tax.account_head

					if i == len(valuation_tax):
						applicable_amount = amount_including_divisional_loss
					else:
						applicable_amount = negative_expense_to_be_booked * (
							valuation_tax[tax.name] / total_valuation_amount
						)
						amount_including_divisional_loss -= applicable_amount

					self.add_gl_entry(
						gl_entries=gl_entries,
						account=account,
						cost_center=tax.cost_center,
						debit=0.0,
						credit=applicable_amount,
						remarks=self.remarks or _("Accounting Entry for Stock"),
						against_account=against_account,
						item=tax,
					)

					i += 1

	def get_asset_gl_entry(self, gl_entries):
		for item in self.get("items"):
			if item.is_fixed_asset:
				if is_cwip_accounting_enabled(item.asset_category):
					self.add_asset_gl_entries(item, gl_entries)
				if flt(item.landed_cost_voucher_amount):
					self.add_lcv_gl_entries(item, gl_entries)
					# update assets gross amount by its valuation rate
					# valuation rate is total of net rate, raw mat supp cost, tax amount, lcv amount per item
					self.update_assets(item, item.valuation_rate)
		return gl_entries

	def add_asset_gl_entries(self, item, gl_entries):
		arbnb_account = self.get_company_default("asset_received_but_not_billed")
		# This returns category's cwip account if not then fallback to company's default cwip account
		cwip_account = get_asset_account(
			"capital_work_in_progress_account", asset_category=item.asset_category, company=self.company
		)

		asset_amount = flt(item.net_amount) + flt(item.item_tax_amount / self.conversion_rate)
		base_asset_amount = flt(item.base_net_amount + item.item_tax_amount)
		remarks = self.get("remarks") or _("Accounting Entry for Asset")

		cwip_account_currency = get_account_currency(cwip_account)
		# debit cwip account
		debit_in_account_currency = (
			base_asset_amount if cwip_account_currency == self.company_currency else asset_amount
		)

		self.add_gl_entry(
			gl_entries=gl_entries,
			account=cwip_account,
			cost_center=item.cost_center,
			debit=base_asset_amount,
			credit=0.0,
			remarks=remarks,
			against_account=arbnb_account,
			debit_in_account_currency=debit_in_account_currency,
			item=item,
		)

		asset_rbnb_currency = get_account_currency(arbnb_account)
		# credit arbnb account
		credit_in_account_currency = (
			base_asset_amount if asset_rbnb_currency == self.company_currency else asset_amount
		)

		self.add_gl_entry(
			gl_entries=gl_entries,
			account=arbnb_account,
			cost_center=item.cost_center,
			debit=0.0,
			credit=base_asset_amount,
			remarks=remarks,
			against_account=cwip_account,
			credit_in_account_currency=credit_in_account_currency,
			item=item,
		)

	def add_lcv_gl_entries(self, item, gl_entries):
		expenses_included_in_asset_valuation = self.get_company_default(
			"expenses_included_in_asset_valuation"
		)
		if not is_cwip_accounting_enabled(item.asset_category):
			asset_account = get_asset_category_account(
				asset_category=item.asset_category, fieldname="fixed_asset_account", company=self.company
			)
		else:
			# This returns company's default cwip account
			asset_account = get_asset_account("capital_work_in_progress_account", company=self.company)

		remarks = self.get("remarks") or _("Accounting Entry for Stock")

		self.add_gl_entry(
			gl_entries=gl_entries,
			account=expenses_included_in_asset_valuation,
			cost_center=item.cost_center,
			debit=0.0,
			credit=flt(item.landed_cost_voucher_amount),
			remarks=remarks,
			against_account=asset_account,
			project=item.project,
			item=item,
		)

		self.add_gl_entry(
			gl_entries=gl_entries,
			account=asset_account,
			cost_center=item.cost_center,
			debit=flt(item.landed_cost_voucher_amount),
			credit=0.0,
			remarks=remarks,
			against_account=expenses_included_in_asset_valuation,
			project=item.project,
			item=item,
		)

	def update_assets(self, item, valuation_rate):
		assets = frappe.db.get_all(
			"Asset", filters={"purchase_receipt": self.name, "item_code": item.item_code}
		)

		for asset in assets:
			frappe.db.set_value("Asset", asset.name, "gross_purchase_amount", flt(valuation_rate))
			frappe.db.set_value("Asset", asset.name, "purchase_receipt_amount", flt(valuation_rate))

	def update_status(self, status):
		self.set_status(update=True, status=status)
		self.notify_update()
		clear_doctype_notifications(self)

	def update_billing_status(self, update_modified=True):
		updated_pr = [self.name]
		po_details = []
		for d in self.get("items"):
			if d.get("purchase_invoice") and d.get("purchase_invoice_item"):
				d.db_set("billed_amt", d.amount, update_modified=update_modified)
			elif d.purchase_order_item:
				po_details.append(d.purchase_order_item)

		if po_details:
			updated_pr += update_billed_amount_based_on_po(po_details, update_modified)

		for pr in set(updated_pr):
			pr_doc = self if (pr == self.name) else frappe.get_doc("Purchase Receipt", pr)
			update_billing_percentage(pr_doc, update_modified=update_modified)

		self.load_from_db()


def update_billed_amount_based_on_po(po_details, update_modified=True):
	po_billed_amt_details = get_billed_amount_against_po(po_details)

	# Get all Purchase Receipt Item rows against the Purchase Order Items
	pr_details = get_purchase_receipts_against_po_details(po_details)

	pr_items = [pr_detail.name for pr_detail in pr_details]
	pr_items_billed_amount = get_billed_amount_against_pr(pr_items)

	updated_pr = []
	for pr_item in pr_details:
		billed_against_po = flt(po_billed_amt_details.get(pr_item.purchase_order_item))

		# Get billed amount directly against Purchase Receipt
		billed_amt_agianst_pr = flt(pr_items_billed_amount.get(pr_item.name, 0))

		# Distribute billed amount directly against PO between PRs based on FIFO
		if billed_against_po and billed_amt_agianst_pr < pr_item.amount:
			pending_to_bill = flt(pr_item.amount) - billed_amt_agianst_pr
			if pending_to_bill <= billed_against_po:
				billed_amt_agianst_pr += pending_to_bill
				billed_against_po -= pending_to_bill
			else:
				billed_amt_agianst_pr += billed_against_po
				billed_against_po = 0

		po_billed_amt_details[pr_item.purchase_order_item] = billed_against_po

		if pr_item.billed_amt != billed_amt_agianst_pr:
			frappe.db.set_value(
				"Purchase Receipt Item",
				pr_item.name,
				"billed_amt",
				billed_amt_agianst_pr,
				update_modified=update_modified,
			)

			updated_pr.append(pr_item.parent)

	return updated_pr


def get_purchase_receipts_against_po_details(po_details):
	# Get Purchase Receipts against Purchase Order Items

	purchase_receipt = frappe.qb.DocType("Purchase Receipt")
	purchase_receipt_item = frappe.qb.DocType("Purchase Receipt Item")

	query = (
		frappe.qb.from_(purchase_receipt)
		.inner_join(purchase_receipt_item)
		.on(purchase_receipt.name == purchase_receipt_item.parent)
		.select(
			purchase_receipt_item.name,
			purchase_receipt_item.parent,
			purchase_receipt_item.amount,
			purchase_receipt_item.billed_amt,
			purchase_receipt_item.purchase_order_item,
		)
		.where(
			(purchase_receipt_item.purchase_order_item.isin(po_details))
			& (purchase_receipt.docstatus == 1)
			& (purchase_receipt.is_return == 0)
		)
		.orderby(CombineDatetime(purchase_receipt.posting_date, purchase_receipt.posting_time))
		.orderby(purchase_receipt.name)
	)

	return query.run(as_dict=True)


def get_billed_amount_against_pr(pr_items):
	# Get billed amount directly against Purchase Receipt

	if not pr_items:
		return {}

	purchase_invoice_item = frappe.qb.DocType("Purchase Invoice Item")

	query = (
		frappe.qb.from_(purchase_invoice_item)
		.select(fn.Sum(purchase_invoice_item.amount).as_("billed_amt"), purchase_invoice_item.pr_detail)
		.where((purchase_invoice_item.pr_detail.isin(pr_items)) & (purchase_invoice_item.docstatus == 1))
		.groupby(purchase_invoice_item.pr_detail)
	).run(as_dict=1)

	return {d.pr_detail: flt(d.billed_amt) for d in query}


def get_billed_amount_against_po(po_items):
	# Get billed amount directly against Purchase Order
	if not po_items:
		return {}

	purchase_invoice_item = frappe.qb.DocType("Purchase Invoice Item")

	query = (
		frappe.qb.from_(purchase_invoice_item)
		.select(fn.Sum(purchase_invoice_item.amount).as_("billed_amt"), purchase_invoice_item.po_detail)
		.where(
			(purchase_invoice_item.po_detail.isin(po_items))
			& (purchase_invoice_item.docstatus == 1)
			& (purchase_invoice_item.pr_detail.isnull())
		)
		.groupby(purchase_invoice_item.po_detail)
	).run(as_dict=1)

	return {d.po_detail: flt(d.billed_amt) for d in query}


def update_billing_percentage(pr_doc, update_modified=True, adjust_incoming_rate=False):
	# Reload as billed amount was set in db directly
	pr_doc.load_from_db()

	# Update Billing % based on pending accepted qty
	total_amount, total_billed_amount = 0, 0
	item_wise_returned_qty = get_item_wise_returned_qty(pr_doc)

	for item in pr_doc.items:
		returned_qty = flt(item_wise_returned_qty.get(item.name))
		returned_amount = flt(returned_qty) * flt(item.rate)
		pending_amount = flt(item.amount) - returned_amount
		total_billable_amount = pending_amount if item.billed_amt <= pending_amount else item.billed_amt

		total_amount += total_billable_amount
		total_billed_amount += flt(item.billed_amt)
		if adjust_incoming_rate:
			adjusted_amt = 0.0
			if item.billed_amt and item.amount:
				adjusted_amt = flt(item.billed_amt) - flt(item.amount)

			item.db_set("rate_difference_with_purchase_invoice", adjusted_amt, update_modified=False)

	percent_billed = round(100 * (total_billed_amount / (total_amount or 1)), 6)
	pr_doc.db_set("per_billed", percent_billed)
	pr_doc.load_from_db()

	if update_modified:
		pr_doc.set_status(update=True)
		pr_doc.notify_update()

	if adjust_incoming_rate:
		adjust_incoming_rate_for_pr(pr_doc)


def adjust_incoming_rate_for_pr(doc):
	doc.update_valuation_rate(reset_outgoing_rate=False)

	for item in doc.get("items"):
		item.db_update()

	doc.docstatus = 2
	doc.update_stock_ledger(allow_negative_stock=True, via_landed_cost_voucher=True)
	doc.make_gl_entries_on_cancel()

	# update stock & gl entries for submit state of PR
	doc.docstatus = 1
	doc.update_stock_ledger(allow_negative_stock=True, via_landed_cost_voucher=True)
	doc.make_gl_entries()
	doc.repost_future_sle_and_gle()


def get_item_wise_returned_qty(pr_doc):
	items = [d.name for d in pr_doc.items]

	return frappe._dict(
		frappe.get_all(
			"Purchase Receipt",
			fields=[
				"`tabPurchase Receipt Item`.purchase_receipt_item",
				"sum(abs(`tabPurchase Receipt Item`.qty)) as qty",
			],
			filters=[
				["Purchase Receipt", "docstatus", "=", 1],
				["Purchase Receipt", "is_return", "=", 1],
				["Purchase Receipt Item", "purchase_receipt_item", "in", items],
			],
			group_by="`tabPurchase Receipt Item`.purchase_receipt_item",
			as_list=1,
		)
	)


@frappe.whitelist()
def make_purchase_invoice(source_name, target_doc=None):
	from erpnext.accounts.party import get_payment_terms_template

	doc = frappe.get_doc("Purchase Receipt", source_name)
	returned_qty_map = get_returned_qty_map(source_name)
	invoiced_qty_map = get_invoiced_qty_map(source_name)

	def set_missing_values(source, target):
		if len(target.get("items")) == 0:
			frappe.throw(_("All items have already been Invoiced/Returned"))

		doc = frappe.get_doc(target)
		doc.payment_terms_template = get_payment_terms_template(
			source.supplier, "Supplier", source.company
		)
		doc.run_method("onload")
		doc.run_method("set_missing_values")
		doc.run_method("calculate_taxes_and_totals")
		doc.set_payment_schedule()

	def update_item(source_doc, target_doc, source_parent):
		target_doc.qty, returned_qty = get_pending_qty(source_doc)
		if frappe.db.get_single_value(
			"Buying Settings", "bill_for_rejected_quantity_in_purchase_invoice"
		):
			target_doc.rejected_qty = 0
		target_doc.stock_qty = flt(target_doc.qty) * flt(
			target_doc.conversion_factor, target_doc.precision("conversion_factor")
		)
		returned_qty_map[source_doc.name] = returned_qty

	def get_pending_qty(item_row):
		qty = item_row.qty
		if frappe.db.get_single_value(
			"Buying Settings", "bill_for_rejected_quantity_in_purchase_invoice"
		):
			qty = item_row.received_qty
		pending_qty = qty - invoiced_qty_map.get(item_row.name, 0)
		returned_qty = flt(returned_qty_map.get(item_row.name, 0))
		if returned_qty:
			if returned_qty >= pending_qty:
				pending_qty = 0
				returned_qty -= pending_qty
			else:
				pending_qty -= returned_qty
				returned_qty = 0
		return pending_qty, returned_qty

	doclist = get_mapped_doc(
		"Purchase Receipt",
		source_name,
		{
			"Purchase Receipt": {
				"doctype": "Purchase Invoice",
				"field_map": {
					"supplier_warehouse": "supplier_warehouse",
					"is_return": "is_return",
					"bill_date": "bill_date",
				},
				"validation": {
					"docstatus": ["=", 1],
				},
			},
			"Purchase Receipt Item": {
				"doctype": "Purchase Invoice Item",
				"field_map": {
					"name": "pr_detail",
					"parent": "purchase_receipt",
					"purchase_order_item": "po_detail",
					"purchase_order": "purchase_order",
					"is_fixed_asset": "is_fixed_asset",
					"asset_location": "asset_location",
					"asset_category": "asset_category",
				},
				"postprocess": update_item,
				"filter": lambda d: get_pending_qty(d)[0] <= 0
				if not doc.get("is_return")
				else get_pending_qty(d)[0] > 0,
			},
			"Purchase Taxes and Charges": {"doctype": "Purchase Taxes and Charges", "add_if_empty": True},
		},
		target_doc,
		set_missing_values,
	)

	doclist.set_onload("ignore_price_list", True)
	return doclist


def get_invoiced_qty_map(purchase_receipt):
	"""returns a map: {pr_detail: invoiced_qty}"""
	invoiced_qty_map = {}

	for pr_detail, qty in frappe.db.sql(
		"""select pr_detail, qty from `tabPurchase Invoice Item`
		where purchase_receipt=%s and docstatus=1""",
		purchase_receipt,
	):
		if not invoiced_qty_map.get(pr_detail):
			invoiced_qty_map[pr_detail] = 0
		invoiced_qty_map[pr_detail] += qty

	return invoiced_qty_map


def get_returned_qty_map(purchase_receipt):
	"""returns a map: {so_detail: returned_qty}"""
	returned_qty_map = frappe._dict(
		frappe.db.sql(
			"""select pr_item.purchase_receipt_item, abs(pr_item.qty) as qty
		from `tabPurchase Receipt Item` pr_item, `tabPurchase Receipt` pr
		where pr.name = pr_item.parent
			and pr.docstatus = 1
			and pr.is_return = 1
			and pr.return_against = %s
	""",
			purchase_receipt,
		)
	)

	return returned_qty_map


@frappe.whitelist()
def make_purchase_return_against_rejected_warehouse(source_name):
	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	return make_return_doc("Purchase Receipt", source_name, return_against_rejected_qty=True)


@frappe.whitelist()
def make_purchase_return(source_name, target_doc=None):
	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	return make_return_doc("Purchase Receipt", source_name, target_doc)


@frappe.whitelist()
def update_purchase_receipt_status(docname, status):
	pr = frappe.get_doc("Purchase Receipt", docname)
	pr.update_status(status)


@frappe.whitelist()
def make_stock_entry(source_name, target_doc=None):
	def set_missing_values(source, target):
		target.stock_entry_type = "Material Transfer"
		target.purpose = "Material Transfer"
		target.set_missing_values()

	doclist = get_mapped_doc(
		"Purchase Receipt",
		source_name,
		{
			"Purchase Receipt": {
				"doctype": "Stock Entry",
			},
			"Purchase Receipt Item": {
				"doctype": "Stock Entry Detail",
				"field_map": {
					"warehouse": "s_warehouse",
					"parent": "reference_purchase_receipt",
					"batch_no": "batch_no",
				},
			},
		},
		target_doc,
		set_missing_values,
	)

	return doclist


@frappe.whitelist()
def make_inter_company_delivery_note(source_name, target_doc=None):
	return make_inter_company_transaction("Purchase Receipt", source_name, target_doc)


def get_item_account_wise_additional_cost(purchase_document):
	landed_cost_vouchers = frappe.get_all(
		"Landed Cost Purchase Receipt",
		fields=["parent"],
		filters={"receipt_document": purchase_document, "docstatus": 1},
	)

	if not landed_cost_vouchers:
		return

	item_account_wise_cost = {}

	for lcv in landed_cost_vouchers:
		landed_cost_voucher_doc = frappe.get_doc("Landed Cost Voucher", lcv.parent)

		# Use amount field for total item cost for manually cost distributed LCVs
		if landed_cost_voucher_doc.distribute_charges_based_on == "Distribute Manually":
			based_on_field = "amount"
		else:
			based_on_field = frappe.scrub(landed_cost_voucher_doc.distribute_charges_based_on)

		total_item_cost = 0

		for item in landed_cost_voucher_doc.items:
			total_item_cost += item.get(based_on_field)

		for item in landed_cost_voucher_doc.items:
			if item.receipt_document == purchase_document:
				for account in landed_cost_voucher_doc.taxes:
					item_account_wise_cost.setdefault((item.item_code, item.purchase_receipt_item), {})
					item_account_wise_cost[(item.item_code, item.purchase_receipt_item)].setdefault(
						account.expense_account, {"amount": 0.0, "base_amount": 0.0}
					)

					if total_item_cost > 0:
						item_account_wise_cost[(item.item_code, item.purchase_receipt_item)][
							account.expense_account
						]["amount"] += (
							account.amount * item.get(based_on_field) / total_item_cost
						)

						item_account_wise_cost[(item.item_code, item.purchase_receipt_item)][
							account.expense_account
						]["base_amount"] += (
							account.base_amount * item.get(based_on_field) / total_item_cost
						)
					else:
						item_account_wise_cost[(item.item_code, item.purchase_receipt_item)][
							account.expense_account
						]["amount"] += item.applicable_charges
						item_account_wise_cost[(item.item_code, item.purchase_receipt_item)][
							account.expense_account
						]["base_amount"] += item.applicable_charges

	return item_account_wise_cost


def on_doctype_update():
	frappe.db.add_index("Purchase Receipt", ["supplier", "is_return", "return_against"])
