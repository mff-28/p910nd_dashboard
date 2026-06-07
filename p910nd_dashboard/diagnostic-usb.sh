#!/bin/bash
# diagnostic-usb.sh — Debug USB printer detection

echo "=== USB Printer Detection Debug ==="
echo ""

echo "1. Device nodes:"
ls -la /dev/usb/lp* 2>/dev/null || echo "  (tidak ada)"
echo ""

echo "2. USB devices (lsusb):"
lsusb
echo ""

echo "3. Sysfs structure untuk lp0:"
for path in /sys/class/usb_printer/lp0 /sys/class/usblp/lp0 /sys/class/usb_printer /sys/class/usblp; do
  if [ -d "$path" ]; then
    echo "  Found: $path"
    ls -la "$path" 2>/dev/null | head -20
  fi
done
echo ""

echo "4. Sysfs device info (jika ada):"
for dev in /sys/class/usb_printer/lp0/device /sys/class/usblp/lp0/device; do
  if [ -L "$dev" ]; then
    echo "  Symlink $dev -> $(readlink -f $dev)"
    REALPATH=$(readlink -f "$dev")
    if [ -d "$REALPATH" ]; then
      echo "    idVendor:  $(cat $REALPATH/idVendor 2>/dev/null || echo 'N/A')"
      echo "    idProduct: $(cat $REALPATH/idProduct 2>/dev/null || echo 'N/A')"
      echo "    manufacturer: $(cat $REALPATH/manufacturer 2>/dev/null || echo 'N/A')"
      echo "    product:      $(cat $REALPATH/product 2>/dev/null || echo 'N/A')"
      echo "    serial:       $(cat $REALPATH/serial 2>/dev/null || echo 'N/A')"
    fi
  fi
done
echo ""

echo "5. Device string via usb_device_get:"
for dev in /dev/usb/lp*; do
  if [ -e "$dev" ]; then
    echo "  Device: $dev"
    udevadm info --name="$dev" 2>/dev/null | grep -E "ID_VENDOR|ID_MODEL|ID_SERIAL" || echo "    (udevadm tidak tersedia)"
  fi
done
echo ""

echo "Done."
