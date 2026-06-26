"""
QRIS Static to Dynamic Converter
Converts a static QRIS string to dynamic by injecting transaction amount.
Based on QRIS (Quick Response Code Indonesian Standard) EMV specification.
"""

import binascii
import qrcode
from io import BytesIO


def crc16_ccitt(data: str) -> str:
    """
    Calculate CRC16-CCITT checksum for QRIS data.
    Polynomial: 0x1021, Initial value: 0xFFFF
    """
    crc = 0xFFFF
    for byte in data.encode('utf-8'):
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc = crc << 1
            crc &= 0xFFFF
    return format(crc, '04X')


def parse_qris(qris_string: str) -> list:
    """
    Parse QRIS string into TLV (Tag-Length-Value) components.
    Returns list of tuples: (tag, length, value)
    """
    components = []
    i = 0
    while i < len(qris_string):
        if i + 4 > len(qris_string):
            break
        tag = qris_string[i:i+2]
        length = int(qris_string[i+2:i+4])
        value = qris_string[i+4:i+4+length]
        components.append((tag, length, value))
        i += 4 + length
    return components


def build_tlv(tag: str, value: str) -> str:
    """Build a TLV string from tag and value."""
    length = len(value)
    return f"{tag}{length:02d}{value}"


def static_to_dynamic(qris_static: str, amount: int, fee: int = 0, fee_type: str = "fixed") -> str:
    """
    Convert static QRIS to dynamic QRIS with specified amount.
    
    Args:
        qris_static: The static QRIS string (from your Mitra Bukalapak QRIS)
        amount: Transaction amount in Rupiah
        fee: Optional service fee
        fee_type: "fixed" (Rupiah) or "percent" (percentage)
    
    Returns:
        Dynamic QRIS string with amount embedded
    """
    # Remove existing CRC (last 4 characters of checksum value, tag 63)
    # The CRC tag is "63" with length "04"
    if qris_static[-8:-4] == "6304":
        qris_without_crc = qris_static[:-4]
    else:
        # Try to find and remove CRC
        qris_without_crc = qris_static[:-8] if "6304" in qris_static[-8:] else qris_static

    # Parse the QRIS components
    components = parse_qris(qris_static)
    
    # Rebuild QRIS with modifications
    new_qris = ""
    amount_added = False
    fee_added = False
    
    for tag, length, value in components:
        if tag == "63":
            # Skip old CRC, we'll add new one at the end
            continue
        elif tag == "54":
            # Replace existing amount tag
            new_qris += build_tlv("54", str(amount))
            amount_added = True
        elif tag == "55":
            # Replace existing fee tag
            if fee > 0:
                if fee_type == "percent":
                    new_qris += build_tlv("55", "01")  # percentage
                    new_qris += build_tlv("56", str(fee))
                else:
                    new_qris += build_tlv("55", "02")  # fixed
                    new_qris += build_tlv("57", str(fee))
                fee_added = True
            continue
        elif tag in ("56", "57"):
            # Skip old fee value, handled above
            continue
        else:
            new_qris += build_tlv(tag, value)
    
    # Add amount if not already present
    if not amount_added:
        # Insert amount (tag 54) before country code (tag 58) or at appropriate position
        # We need to rebuild with amount in correct position
        new_qris_with_amount = ""
        parts = parse_qris(new_qris + "6304FFFF")  # temp CRC for parsing
        
        inserted = False
        for tag, length, value in parts:
            if tag == "63":
                continue
            if tag == "58" and not inserted:
                # Insert amount before country code
                new_qris_with_amount += build_tlv("54", str(amount))
                inserted = True
            new_qris_with_amount += build_tlv(tag, value)
        
        if not inserted:
            new_qris_with_amount += build_tlv("54", str(amount))
        
        new_qris = new_qris_with_amount
    
    # Add fee if specified and not already added
    if fee > 0 and not fee_added:
        if fee_type == "percent":
            new_qris += build_tlv("55", "01")
            new_qris += build_tlv("56", str(fee))
        else:
            new_qris += build_tlv("55", "02")
            new_qris += build_tlv("57", str(fee))
    
    # Add CRC tag placeholder and calculate
    new_qris += "6304"
    crc = crc16_ccitt(new_qris)
    new_qris += crc
    
    return new_qris


def generate_qr_image(qris_string: str) -> BytesIO:
    """
    Generate QR code image from QRIS string.
    Returns BytesIO buffer containing PNG image.
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(qris_string)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    
    return buffer


def validate_qris(qris_string: str) -> bool:
    """
    Validate QRIS string by checking CRC16.
    """
    if len(qris_string) < 8:
        return False
    
    # Get the CRC from the string (last 4 characters)
    provided_crc = qris_string[-4:]
    
    # Calculate CRC for the string without the CRC value
    data_without_crc = qris_string[:-4]
    calculated_crc = crc16_ccitt(data_without_crc)
    
    return provided_crc.upper() == calculated_crc.upper()


def get_merchant_name(qris_string: str) -> str:
    """Extract merchant name from QRIS string (tag 59)."""
    components = parse_qris(qris_string)
    for tag, length, value in components:
        if tag == "59":
            return value
    return "Unknown"


def get_merchant_city(qris_string: str) -> str:
    """Extract merchant city from QRIS string (tag 60)."""
    components = parse_qris(qris_string)
    for tag, length, value in components:
        if tag == "60":
            return value
    return "Unknown"


# Example usage
if __name__ == "__main__":
    # Example static QRIS (replace with your actual Mitra Bukalapak QRIS)
    sample_qris = "00020101021126570011ID.DANA.WWW01189360091400000000010215000000000000000303UMI51440014ID.CO.QRIS.WWW0215ID10210000000000303UMI5204541153033605802ID5909TestName6007Jakarta61051012062070703A0163049A25"
    
    print("=== QRIS Static to Dynamic Converter ===")
    print(f"Original QRIS valid: {validate_qris(sample_qris)}")
    print(f"Merchant: {get_merchant_name(sample_qris)}")
    print(f"City: {get_merchant_city(sample_qris)}")
    
    # Convert to dynamic with amount
    dynamic_qris = static_to_dynamic(sample_qris, amount=50000)
    print(f"\nDynamic QRIS (Rp 50.000):")
    print(f"Valid: {validate_qris(dynamic_qris)}")
    print(f"Data: {dynamic_qris}")
