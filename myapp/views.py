from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.core.exceptions import PermissionDenied
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.db.models import Sum, F, DecimalField  # Used for dashboard reports
from django.contrib import messages

# QR Code Libraries
import qrcode
from io import BytesIO
from base64 import b64encode
from decimal import Decimal

# Import models and forms
from .models import Item, Order, OrderItem
from .forms import ItemForm
from marketing.tracking import record_event


def landing(request):
    """Public landing page for FoodApp."""
    return render(request, 'myapp/landing.html')


# --- Admin/Staff Views (Now Multi-Tenant by filtering by request.user) ---

@login_required(login_url='login')
def index(request):
    """Staff Menu Management List (Shows ONLY items created by the logged-in user)"""
    # FILTER: Only show items created by the current user
    item_list = Item.objects.filter(user_name=request.user)
    context = {
        'item_list': item_list
    }
    return render(request, "myapp/index.html", context)


@login_required(login_url='login')
def detail(request, id):
    # FILTER: Only get item if it belongs to the current user
    item = get_object_or_404(Item, id=id, user_name=request.user)
    return render(request, 'myapp/detail.html', {'item': item})


@login_required(login_url='login')
def create_item(request):
    form = ItemForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        item = form.save(commit=False)
        # ASSIGN: Set the creator of the item to the current user
        item.user_name = request.user
        item.save()
        return redirect('myapp:index')
    return render(request, 'myapp/item_form.html', {'form': form, 'title': 'Add New Food Item'})


@login_required(login_url='login')
def update_item(request, id):
    # FILTER: Only get item if it belongs to the current user
    item = get_object_or_404(Item, id=id, user_name=request.user)
    form = ItemForm(request.POST or None, instance=item)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect('myapp:detail', id=item.id)
    return render(request, 'myapp/item_form.html', {'form': form, 'item': item, 'title': f'Update {item.item_name}'})


@login_required(login_url='login')
def delete_item(request, id):
    # FILTER: Only get item if it belongs to the current user
    item = get_object_or_404(Item, id=id, user_name=request.user)
    if request.method == 'POST':
        item.delete()
        return redirect('myapp:index')
    return render(request, 'myapp/item_delete.html', {'item': item})


# --- QR Code and Dashboard Logic ---

@login_required(login_url='login')
def staff_dashboard(request):
    """Dashboard for staff to view orders, revenue, and item sales FOR THEIR RESTAURANT."""
    # 1. Base query: Orders that contain items owned by the current user
    all_orders_for_user = Order.objects.filter(
        orderitem__item__user_name=request.user
    ).distinct()

    # 2. Active Orders
    active_orders = all_orders_for_user.filter(
        status__in=['Pending', 'Preparing']
    ).order_by('-created_at')

    # 3. Totals and Revenue
    total_orders_count = all_orders_for_user.count()
    # FIX: Revenue is computed from THIS vendor's line items only.
    # Order.total_price covers the whole (possibly mixed-vendor) order, so summing it
    # double-counts when a paid order contains items from more than one restaurant.
    total_revenue = OrderItem.objects.filter(
        item__user_name=request.user,
        order__is_paid=True
    ).aggregate(
        total=Sum(F('item_price') * F('quantity'), output_field=DecimalField())
    )['total'] or Decimal('0.00')

    # 4. Item Sales Report (Filter OrderItems by the Item's owner)
    item_sales_report = OrderItem.objects.filter(
        item__user_name=request.user,
        order__is_paid=True  # Only count paid orders for revenue/sales report
    ).values('item_name').annotate(
        total_quantity=Sum('quantity'),
        total_revenue=Sum(F('item_price') * F('quantity'), output_field=DecimalField())
    ).order_by('-total_quantity')

    context = {
        'active_orders': active_orders,
        'total_orders_count': total_orders_count,
        'total_revenue': total_revenue,
        'item_sales_report': item_sales_report,
    }
    return render(request, 'myapp/staff_dashboard.html', context)


@login_required(login_url='login')
def generate_qr_code(request, table_id):
    """Generates a QR code for a specific table URL."""
    # Include the staff user's username in the URL so the menu view can filter.
    username = request.user.username
    relative_url = reverse('myapp:menu_with_table', kwargs={'username': username, 'table_id': table_id})
    full_url = request.build_absolute_uri(relative_url)

    # 1. Create QR code object
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(full_url)
    qr.make(fit=True)

    # 2. Create an image from the QR code and save to a buffer
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")

    # 3. Encode the image data to Base64
    qr_code_base64 = b64encode(buffer.getvalue()).decode('utf-8')

    context = {
        'table_id': table_id,
        'qr_code_data': qr_code_base64,
        'full_url': full_url
    }
    return render(request, 'myapp/qr_generator.html', context)


@login_required(login_url='login')
@require_POST
def update_order_status(request, order_id, new_status):
    # POST-only: state-changing action (marks orders paid) must never fire on GET navigation.
    # FILTER: Only allow updating orders that contain items owned by the current user
    order = get_object_or_404(
        Order.objects.filter(orderitem__item__user_name=request.user).distinct(),
        id=order_id
    )

    if new_status in [choice[0] for choice in Item.STATUS_CHOICES]:
        order.status = new_status
        if new_status == 'Completed':
            order.is_paid = True
        order.save()
    return redirect('myapp:staff_dashboard')


# --- Customer Facing Views (Public menu now filters by item availability, NOT ownership) ---

def menu(request, username=None, table_id=None):
    """Public view for customers, filters items by is_available=True AND optionally by a restaurant/user if provided in the URL."""
    if table_id:
        request.session['table_number'] = table_id
        # Store the current menu URL for redirection after adding an item to cart
        request.session['menu_redirect_url'] = reverse(
            'myapp:menu_with_table', kwargs={'username': username, 'table_id': table_id}
        )
    else:
        # Clear the table-specific redirect URL if accessing the root menu
        if 'menu_redirect_url' in request.session:
            del request.session['menu_redirect_url']

    # Filter items:
    # 1. Always filter by is_available=True
    item_list = Item.objects.filter(is_available=True)

    # 2. If a username is provided in the URL (from a QR scan), filter by that user's items.
    if username:
        item_list = item_list.filter(user_name__username=username)  # Filter by the username from the URL
    context = {
        'item_list': item_list,
        'table_number': request.session.get('table_number', 'N/A')
    }
    response = render(request, 'myapp/menu.html', context)
    # Funnel: menu view (vendor = the menu owner on a QR-scanned menu, None on the shared menu)
    record_event(request, 'menu_view', vendor=item_list.first().user_name if item_list.exists() else None)
    return response

def add_to_cart(request, item_id):
    """Handles adding item to session-based cart, redirects back to menu with a message.

    The session stores ONLY {item_id: {'quantity': n}}. Names and prices are re-read
    from the database at cart view / checkout so the customer always pays the
    current DB price and deleted/unavailable items never reach an order.
    """
    item = get_object_or_404(Item, id=item_id, is_available=True)
    cart = request.session.get('cart', {})

    item_id_str = str(item_id)
    if item_id_str in cart:
        cart[item_id_str]['quantity'] += 1
    else:
        cart[item_id_str] = {'quantity': 1}

    request.session['cart'] = cart
    # Add success message (Notification type)
    messages.success(request, f"{item.item_name} added to cart!")

    # Funnel: add_to_cart event (vendor = the item owner)
    record_event(request, 'add_to_cart', vendor=item.user_name, item=item)

    # Redirect back to the menu (main or table-specific)
    # Funnel: add_to_cart event (vendor = the item owner)
    record_event(request, 'add_to_cart', vendor=item.user_name, item=item)
    messages.success(request, f"{item.item_name} added to cart!")

    # Redirect back to the menu (main or table-specific)
    menu_redirect_url = request.session.get('menu_redirect_url', reverse('myapp:menu'))
    return redirect(menu_redirect_url)


@require_POST
def remove_from_cart(request, item_id):
    """POST-only: removes a line (the selected item) from the cart entirely."""
    cart = request.session.get('cart', {})
    key = str(item_id)
    if key in cart:
        del cart[key]
        request.session['cart'] = cart
        request.session.modified = True
        messages.info(request, "Item removed from cart.")
    return redirect('myapp:view_cart')


@require_POST
def clear_cart(request):
    """POST-only: empties the whole cart."""
    if 'cart' in request.session:
        del request.session['cart']
        request.session.modified = True
        messages.info(request, "Cart cleared.")
    return redirect('myapp:view_cart')


def view_cart(request):
    """Displays the current cart contents.

    Names/prices are re-read from the DB (session holds only quantities).
    Stale entries (item deleted or unavailable) are dropped from the session here.
    """
    cart = request.session.get('cart', {})
    cart_items = []
    stale_keys = []

    if cart:
        db_items = Item.objects.filter(id__in=[int(k) for k in cart.keys()])
        items_by_key = {str(i.id): i for i in db_items}
        for key, entry in cart.items():
            db_item = items_by_key.get(key)
            if db_item is None or not db_item.is_available:
                stale_keys.append(key)
                continue
            qty = int(entry.get('quantity', 0))
            if qty <= 0:
                stale_keys.append(key)
                continue
            cart_items.append({
                'id': db_item.id,
                'name': db_item.item_name,
                'price': db_item.item_price,  # Decimal, straight from the DB
                'quantity': qty,
                'line_total': db_item.item_price * qty,
            })

    if stale_keys:
        for key in stale_keys:
            cart.pop(key, None)
        request.session['cart'] = cart
        messages.warning(request, "Some items in your cart are no longer available and were removed.")

    cart_total = sum((ci['line_total'] for ci in cart_items), Decimal('0.00'))
    context = {
        'cart_items': cart_items,
        'cart_total': cart_total,
        'table_number': request.session.get('table_number', 'N/A')
    }
    return render(request, 'myapp/cart.html', context)


def checkout(request):
    """Creates a new Order from the cart and clears the cart.

    The DB is the only source of truth: items are re-fetched and prices recomputed
    at checkout. Session-stored names/prices are never trusted; deleted or
    unavailable items are dropped instead of being charged.
    """
    cart = request.session.get('cart', {})
    if not cart:
        return redirect('myapp:menu')

    if request.method == 'POST':
        # Re-fetch all cart items in ONE query; skip deleted/unavailable items.
        db_items = Item.objects.filter(id__in=[int(k) for k in cart.keys()], is_available=True)
        items_by_key = {str(i.id): i for i in db_items}

        validated = []  # (db_item, quantity)
        total = Decimal('0.00')
        for item_id_str, entry in cart.items():
            db_item = items_by_key.get(item_id_str)
            if db_item is None:
                continue  # deleted or no longer available — dropped, never charged
            try:
                qty = int(entry.get('quantity', 0))
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            validated.append((db_item, qty))
            total += db_item.item_price * qty

        if not validated:
            del request.session['cart']
            request.session.modified = True
            messages.error(request, "Your cart is empty (items are no longer available).")
            return redirect('myapp:menu')

        new_order = Order.objects.create(
            user=request.user if request.user.is_authenticated else None,
            table_number=request.session.get('table_number'),
            total_price=total,
            status='Pending',
            is_paid=False
        )

        OrderItem.objects.bulk_create([
            OrderItem(
                order=new_order,
                item=db_item,
                item_name=db_item.item_name,
                item_price=db_item.item_price,
                quantity=qty,
            )
            for db_item, qty in validated
        ])

        del request.session['cart']
        request.session.modified = True

        # FIX: Get the correct menu redirect URL from session, default to generic menu
        menu_redirect_url = request.session.get('menu_redirect_url', reverse('myapp:menu'))

        return render(request, 'myapp/checkout_success.html', {
            'order': new_order,
            'menu_redirect_url': menu_redirect_url  # Pass URL to template
        })

    return redirect('myapp:view_cart')
