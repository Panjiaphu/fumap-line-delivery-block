# FUMAP GO MVP Step 2

This version separates desktop and mobile/app-ready frontend structure.

## Structure

```text
app.py
templates/
  desktop/
  mobile/
static/
  common/
  desktop/
  mobile/
```

## Render

- Language: Python 3
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`
- Health: `/health`

## View switch

Add query param:

```text
?view=desktop
?view=mobile
```

Driver and customer routes default to mobile. Admin/block routes default to desktop.

## MVP flow

1. `/wallet`
2. `/store`
3. `/driver?view=mobile`
4. `/customer?view=mobile`
5. `/block`
6. `/dispute?order_code=FG-...`


## Step 3 added

- `smartroad.py`
- Smart Road score on order creation
- Red/yellow lane by odd/even house number
- 250 TWD/h efficiency filter
- Prevent duplicate driver blocks by status transition
- One-link customer confirmation:
  `/customer/confirm/<ORDER_CODE>?view=mobile`

Customer no longer needs to manually enter Order Code if they use the confirmation link.


## Step 4 added: i18n foundation

- Traditional Chinese / English language switch
- `lang=zh` or `lang=en`
- `/set-lang/zh` and `/set-lang/en`
- `t("key")` helper in templates
- Desktop/mobile language buttons

Example:

```text
/store?view=desktop&lang=zh
/store?view=desktop&lang=en
/driver?view=mobile&lang=en
/customer/confirm/<ORDER_CODE>?view=mobile&lang=en
```



## Step 5: Customer no longer enters order code

Primary customer flow:

```text
Store creates order
→ system generates Customer Delivery Link
→ customer opens /c/<ORDER_CODE>?view=mobile
→ customer enters Delivery PIN only
→ Delivery Proof Block is created
```

Routes:

```text
/c/<ORDER_CODE>?view=mobile
/customer/confirm/<ORDER_CODE>?view=mobile
```

The old `/customer` page no longer asks customer to type Order Code. It only shows recent delivery links for demo/testing.



## Step 6: fix quoted links

Fixed invalid template links such as:

```html
href=\"/store?view=desktop&lang=zh\"
```

which caused browser URLs like:

```text
/" /store?view=desktop&lang=zh "
```

Correct links now render as:

```html
href="/store?view=desktop&lang=zh"
```


## Step 7: Customer Delivery Code visibility

Important distinction:

- Store Wallet PIN = store authentication/signature, not used by customer
- Pickup Code = store + driver pickup confirmation
- Customer Delivery Code = customer confirms delivery

MVP behavior:

- Store dashboard shows `delivery_code_demo` so the store can give it to the customer for testing.
- Customer confirmation page only asks for Customer Delivery Code.
- Driver does **not** see the Customer Delivery Code in advance.
- Production version should send the Customer Delivery Code by LINE/SMS and store only hash.


## Step 8: Receiver phone last-4 + face/photo delivery

New business logic:

- Orderer and receiver are separated.
- If A orders for B, the delivery code uses B's phone last 4 digits.
- Driver only sees masked phone, e.g. `09****2345`.
- Driver can complete by:
  - receiver phone last 4 code
  - face-to-face confirmation
  - photo proof
- Store can create order with verification method:
  - AUTO
  - PHONE_LAST4
  - RANDOM_CODE
  - FACE_TO_FACE
  - PHOTO

Test case:

```text
Orderer A phone: 0912341234
Receiver B phone: 0909112345
Delivery Code = 2345
```


## Step 9: Store simple mode + optional A orders for B

Store form now defaults to:

```text
Orderer = Receiver
```

The store only fills receiver name/phone/address for normal orders.

If A orders for B, the store checks:

```text
訂購人與收件人不同
```

Then fills orderer A details. Delivery code still uses receiver B phone last 4 digits.
