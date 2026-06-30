#!/bin/bash
# SSL setup script for the Unipile LinkedIn Outreach API.
# Reused from hiringday_linkedin_login2 and adapted for the outreach backend.

set -e

DEFAULT_DOMAIN="linkedin-server.libingo.io"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=============================================="
echo "SSL Setup for Unipile Outreach API"
echo "=============================================="
echo ""
echo "Choose an option:"
echo "1) Self-signed certificate (development/testing)"
echo "2) Let's Encrypt for $DEFAULT_DOMAIN (production)"
echo "3) Let's Encrypt for a custom domain"
echo "4) Remove existing certificates only"
echo ""
read -p "Enter option (1-4): " OPTION

mkdir -p "$PROJECT_DIR/ssl"
mkdir -p "$PROJECT_DIR/certbot/conf"
mkdir -p "$PROJECT_DIR/certbot/www"

case $OPTION in
    1)
        echo ""
        echo "Generating self-signed certificate..."
        openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
            -keyout "$PROJECT_DIR/ssl/key.pem" \
            -out "$PROJECT_DIR/ssl/cert.pem" \
            -subj "/CN=$DEFAULT_DOMAIN/O=Libingo/C=IN" \
            -addext "subjectAltName=DNS:$DEFAULT_DOMAIN,DNS:localhost,IP:127.0.0.1"
        echo ""
        echo "Self-signed certificate created."
        echo "Use: docker compose --profile production up -d"
        echo "Your API will be available at https://$DEFAULT_DOMAIN (browser warning is normal)."
        ;;

    2)
        DOMAIN="$DEFAULT_DOMAIN"
        read -p "Enter email for Let's Encrypt notifications: " EMAIL

        echo ""
        echo "Stopping any existing nginx container..."
        docker compose --profile production stop nginx 2>/dev/null || true

        echo "Removing old certificates for $DOMAIN (if any)..."
        rm -f "$PROJECT_DIR/ssl/cert.pem" "$PROJECT_DIR/ssl/key.pem"
        rm -rf "$PROJECT_DIR/certbot/conf/live/$DOMAIN"
        rm -rf "$PROJECT_DIR/certbot/conf/archive/$DOMAIN"
        rm -rf "$PROJECT_DIR/certbot/conf/renewal/$DOMAIN.conf"

        echo "Requesting Let's Encrypt certificate for $DOMAIN..."
        docker compose run --rm --entrypoint "certbot certonly --standalone --preferred-challenges http -d $DOMAIN --email $EMAIL --agree-tos --no-eff-email" certbot

        echo "Creating symlinks for nginx..."
        ln -sf "../certbot/conf/live/$DOMAIN/fullchain.pem" "$PROJECT_DIR/ssl/cert.pem"
        ln -sf "../certbot/conf/live/$DOMAIN/privkey.pem" "$PROJECT_DIR/ssl/key.pem"

        echo ""
        echo "Certificate obtained successfully."
        echo "Start with: docker compose --profile production up -d"
        ;;

    3)
        read -p "Enter your custom domain: " DOMAIN
        read -p "Enter email for Let's Encrypt notifications: " EMAIL

        if [ -z "$DOMAIN" ] || [ -z "$EMAIL" ]; then
            echo "Domain and email are required."
            exit 1
        fi

        echo ""
        echo "Stopping any existing nginx container..."
        docker compose --profile production stop nginx 2>/dev/null || true

        echo "Removing old certificates for $DOMAIN (if any)..."
        rm -f "$PROJECT_DIR/ssl/cert.pem" "$PROJECT_DIR/ssl/key.pem"
        rm -rf "$PROJECT_DIR/certbot/conf/live/$DOMAIN"
        rm -rf "$PROJECT_DIR/certbot/conf/archive/$DOMAIN"
        rm -rf "$PROJECT_DIR/certbot/conf/renewal/$DOMAIN.conf"

        echo "Updating nginx.conf domain..."
        sed -i "s/linkedin-server\.libingo\.io/$DOMAIN/g" "$PROJECT_DIR/nginx.conf"

        echo "Requesting Let's Encrypt certificate for $DOMAIN..."
        docker compose run --rm --entrypoint "certbot certonly --standalone --preferred-challenges http -d $DOMAIN --email $EMAIL --agree-tos --no-eff-email" certbot

        echo "Creating symlinks for nginx..."
        ln -sf "../certbot/conf/live/$DOMAIN/fullchain.pem" "$PROJECT_DIR/ssl/cert.pem"
        ln -sf "../certbot/conf/live/$DOMAIN/privkey.pem" "$PROJECT_DIR/ssl/key.pem"

        echo ""
        echo "Certificate obtained successfully."
        echo "Start with: docker compose --profile production up -d"
        ;;

    4)
        echo ""
        read -p "Enter domain to remove (default: $DEFAULT_DOMAIN): " DOMAIN
        DOMAIN=${DOMAIN:-$DEFAULT_DOMAIN}
        rm -f "$PROJECT_DIR/ssl/cert.pem" "$PROJECT_DIR/ssl/key.pem"
        rm -rf "$PROJECT_DIR/certbot/conf/live/$DOMAIN"
        rm -rf "$PROJECT_DIR/certbot/conf/archive/$DOMAIN"
        rm -rf "$PROJECT_DIR/certbot/conf/renewal/$DOMAIN.conf"
        echo "Certificates for $DOMAIN removed."
        ;;

    *)
        echo "Invalid option."
        exit 1
        ;;
esac

echo ""
echo "Done."
