#This file is part of Tryton.  The COPYRIGHT file at the top level of
#this repository contains the full copyright notices and license terms.
from trytond.model import Workflow, ModelView, ModelSQL, fields
from trytond.pyson import Eval, If
from trytond.pool import Pool, PoolMeta
from trytond.transaction import Transaction


__all__ = ['Configuration', 'ShipmentDrop', 'Move']
__metaclass__ = PoolMeta


class Configuration:
    __name__ = 'stock.configuration'

    shipment_drop_sequence = fields.Property(fields.Many2One('ir.sequence',
            'Drop Shipment Sequence', domain=[
                ('company', 'in',
                    [Eval('context', {}).get('company'), False]),
                ('code', '=', 'stock.shipment.drop'),
                ], required=True))


class ShipmentDrop(Workflow, ModelSQL, ModelView):
    "Drop Shipment"
    __name__ = 'stock.shipment.drop'

    effective_date = fields.Date('Effective Date', readonly=True)
    planned_date = fields.Date('Planned Date', states={
            'readonly': Eval('state') != 'draft',
            }, depends=['state'])
    company = fields.Many2One('company.company', 'Company', required=True,
        states={
            'readonly': Eval('state') != 'draft',
            },
        domain=[
            ('id', If(Eval('context', {}).contains('company'), '=', '!='),
                Eval('context', {}).get('company', 0)),
            ],
        depends=['state'])
    reference = fields.Char('Reference', select=1,
        states={
            'readonly': Eval('state') != 'draft',
            }, depends=['state'])
    supplier = fields.Many2One('party.party', 'Supplier',
        states={
            'readonly': (((Eval('state') != 'draft') | Eval('moves'))
                & Eval('supplier')),
            }, on_change=['supplier'], required=True,
        depends=['state', 'moves', 'supplier'])
    contact_address = fields.Many2One('party.address', 'Contact Address',
        states={
            'readonly': Eval('state') != 'draft',
            },
        domain=[('party', '=', Eval('supplier'))],
        depends=['state', 'supplier'])
    customer = fields.Many2One('party.party', 'Customer', required=True,
        states={
            'readonly': (((Eval('state') != 'draft') | Eval('moves'))
                & Eval('customer')),
            }, on_change=['customer'],
        depends=['state', 'moves'])
    delivery_address = fields.Many2One('party.address', 'Delivery Address',
        required=True,
        states={
            'readonly': Eval('state') != 'draft',
            },
        domain=[('party', '=', Eval('customer'))],
        depends=['state', 'customer'])
    moves = fields.One2Many('stock.move', 'shipment', 'Moves',
        add_remove=[
            ('shipment', '=', None),
            ('state', '=', 'draft'),
            ('supplier', '=', Eval('supplier')),
            ('customer_drop', '=', Eval('customer')),
            ],
        domain=[
            ('company', '=', Eval('company')),
            ('from_location.type', '=', 'supplier'),
            ('to_location.type', '=', 'customer'),
            ],
        states={
            'readonly': Eval('state').in_(['waiting', 'done', 'cancel']),
            },
        depends=['state', 'company', 'supplier', 'customer'])
    code = fields.Char('Code', select=1, readonly=True)
    state = fields.Selection([
            ('draft', 'Draft'),
            ('waiting', 'Waiting'),
            ('done', 'Done'),
            ('cancel', 'Canceled'),
            ], 'State', readonly=True)

    @classmethod
    def __setup__(cls):
        super(ShipmentDrop, cls).__setup__()
        cls.__rpc__.update({
                'button_draft': True,
                })
        cls._transitions |= set((
                ('draft', 'waiting'),
                ('waiting', 'done'),
                ('draft', 'cancel'),
                ('waiting', 'cancel'),
                ('waiting', 'draft'),
                ('cancel', 'draft'),
                ))
        cls._buttons.update({
                'cancel': {
                    'invisible': Eval('state').in_(['cancel', 'done']),
                    },
                'draft': {
                    'invisible': ~Eval('state').in_(['cancel', 'draft',
                            'waiting']),
                    'icon': If(Eval('state') == 'cancel',
                        'tryton-clear', 'tryton-go-previous'),
                    },
                'wait': {
                    'invisible': Eval('state') != 'draft',
                    },
                'done': {
                    'invisible': Eval('state') != 'waiting',
                    },
                })
        cls._error_messages.update({
                'reset_move': ('You cannot reset to draft move "%s" which was '
                    'generated by a sale or a purchase.'),
                })

    @staticmethod
    def default_state():
        return 'draft'

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    def on_change_supplier(self):
        if self.supplier:
            address = self.supplier.address_get()
            if address:
                return {'contact_address': address.id}
        return {'contact_address': None}

    def on_change_customer(self):
        if self.customer:
            address = self.customer.address_get(type='delivery')
            if address:
                return {'delivery_address': address.id}
        return {'delivery_address': False}

    def _get_move_planned_date(self):
        '''
        Return the planned date for moves
        '''
        return self.planned_date

    @classmethod
    def _set_move_planned_date(cls, shipments):
        '''
        Set planned date of moves for the shipments
        '''
        Move = Pool().get('stock.move')
        for shipment in shipments:
            planned_date = shipment._get_move_planned_date()
            Move.write([m for m in shipment.moves
                    if m.state not in ('assigned', 'done', 'cancel')], {
                    'planned_date': planned_date,
                    })

    @classmethod
    def create(cls, vlist):
        pool = Pool()
        Sequence = pool.get('ir.sequence')
        Config = pool.get('stock.configuration')

        vlist = [x.copy() for x in vlist]
        config = Config(1)
        for values in vlist:
            values['code'] = Sequence.get_id(config.shipment_drop_sequence)
        shipments = super(ShipmentDrop, cls).create(vlist)
        cls._set_move_planned_date(shipments)
        return shipments

    @classmethod
    def write(cls, shipments, values):
        pool = Pool()
        Purchase = pool.get('purchase.purchase')
        PurchaseLine = pool.get('purchase.line')
        Sale = pool.get('sale.sale')
        SaleLine = pool.get('sale.line')

        result = super(ShipmentDrop, cls).write(shipments, values)
        cls._set_move_planned_date(shipments)

        if values.get('state', '') in ('done', 'cancel'):
            with Transaction().set_user(0, set_context=True):
                purchases = set()
                move_ids = [m.id for s in shipments for m in s.moves]
                purchase_lines = PurchaseLine.search([
                        ('moves', 'in', move_ids),
                        ])
                purchases = list(set(l.purchase for l in purchase_lines or []))
                Purchase.process(purchases)

                sale_lines = SaleLine.search([
                        ('moves', 'in', move_ids),
                        ])
                sales = list(set(l.sale for l in sale_lines or []))
                Sale.process(sales)
        return result

    @classmethod
    def copy(cls, shipments, default=None):
        if default is None:
            default = {}
        default = default.copy()
        default['moves'] = None
        return super(ShipmentDrop, cls).copy(shipments, default=default)

    @classmethod
    @ModelView.button
    @Workflow.transition('cancel')
    def cancel(cls, shipments):
        Move = Pool().get('stock.move')
        Move.cancel([m for s in shipments for m in s.moves])

    @classmethod
    @ModelView.button
    @Workflow.transition('draft')
    def draft(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')
        PurchaseLine = pool.get('purchase.line')
        SaleLine = pool.get('sale.line')
        for shipment in shipments:
            for move in shipment.moves:
                if (move.state == 'cancel'
                        and isinstance(move.origin, (PurchaseLine, SaleLine))):
                    cls.raise_user_error('reset_move', (move.rec_name,))
        Move.draft([m for s in shipments for m in s.moves])

    @classmethod
    @ModelView.button
    @Workflow.transition('waiting')
    def wait(cls, shipments):
        pass

    @classmethod
    @ModelView.button
    @Workflow.transition('done')
    def done(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')
        Date = pool.get('ir.date')
        Move.do([m for s in shipments for m in s.moves])
        cls.write(shipments, {
                'effective_date': Date.today(),
                })


class Move:
    __name__ = 'stock.move'

    customer_drop = fields.Function(fields.Many2One('party.party',
            'Drop Customer'), 'get_customer_drop',
        searcher='search_customer_drop')

    @classmethod
    def _get_shipment(cls):
        models = super(Move, cls)._get_shipment()
        models.append('stock.shipment.drop')
        return models

    def get_customer_drop(self, name):
        PurchaseLine = Pool().get('purchase.line')
        if (isinstance(self.origin, PurchaseLine)
                and self.origin.purchase.customer):
            return self.origin.purchase.customer.id

    @classmethod
    def search_customer_drop(cls, name, clause):
        return [('origin.purchase.customer',) + tuple(clause[1:])
            + ('purchase.line',)]