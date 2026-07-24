import os
from datetime import datetime, timedelta
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

CERTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'certs')
os.makedirs(CERTS_DIR, exist_ok=True)

def generate_self_signed_cert(cert_filename, key_filename, common_name):
    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Generate certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])
    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        private_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.utcnow()
    ).not_valid_after(
        # Valid for 1 year
        datetime.utcnow() + timedelta(days=365)
    ).add_extension(
        x509.SubjectAlternativeName([x509.DNSName(common_name), x509.DNSName("localhost"), x509.DNSName("127.0.0.1")]),
        critical=False,
    ).sign(private_key, hashes.SHA256())

    # Write cert
    cert_path = os.path.join(CERTS_DIR, cert_filename)
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    # Write private key
    key_path = os.path.join(CERTS_DIR, key_filename)
    with open(key_path, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ))
    
    # Change permissions for postgres (needs 0600 on unix, but for windows local testing it's fine,
    # in docker we'll map it and chmod it in an entrypoint if needed, or docker handles it).
    print(f"Generated {cert_path} and {key_path}")

if __name__ == "__main__":
    generate_self_signed_cert("postgres.crt", "postgres.key", "muhafiz-postgres")
    generate_self_signed_cert("vllm.crt", "vllm.key", "muhafiz-vllm")
