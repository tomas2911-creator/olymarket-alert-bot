"""Script para generar API credentials de Polymarket CLOB.

Uso:
  1. Instala dependencia: pip install py-clob-client
  2. Ejecuta: python generate_api_keys.py
  3. Te pedirá tu private key (la de tu wallet de Polygon/MetaMask)
  4. Te dará las 4 credenciales para pegar en el dashboard
"""
import getpass

def main():
    print("\n=== Generador de API Keys para Polymarket ===\n")
    print("Necesitas la Private Key de tu wallet de Polygon.")
    print("(La misma wallet que conectaste a Polymarket)\n")

    pk = getpass.getpass("Pega tu Private Key (no se muestra en pantalla): ").strip()

    # Limpiar
    if pk.startswith("0x"):
        pk = pk[2:]

    if len(pk) != 64:
        print(f"\n❌ La private key debe tener 64 caracteres hex (sin 0x). La tuya tiene {len(pk)}.")
        return

    print("\nGenerando credenciales...")

    try:
        from py_clob_client.client import ClobClient

        client = ClobClient(
            "https://clob.polymarket.com",
            key=pk,
            chain_id=137,
        )

        # Crear o derivar API credentials
        creds = client.create_or_derive_api_creds()

        print("\n" + "=" * 50)
        print("✅ CREDENCIALES GENERADAS EXITOSAMENTE")
        print("=" * 50)
        print(f"\n  Private Key:   0x{pk[:6]}...{pk[-4:]} (la que ya tienes)")
        print(f"  API Key:       {creds.api_key}")
        print(f"  API Secret:    {creds.api_secret}")
        print(f"  Passphrase:    {creds.api_passphrase}")
        print("\n" + "=" * 50)
        print("\nPega estas 4 credenciales en tu dashboard → Crypto Arb → Autotrading")
        print("\n⚠️  NUNCA compartas estas credenciales con nadie.")
        print("⚠️  La Private Key da acceso TOTAL a tus fondos.\n")

    except ImportError:
        print("\n❌ Falta instalar py-clob-client:")
        print("   pip install py-clob-client")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("\nAsegúrate de que:")
        print("  1. La Private Key es correcta")
        print("  2. Tienes conexión a internet")
        print("  3. py-clob-client está instalado: pip install py-clob-client")


if __name__ == "__main__":
    main()
