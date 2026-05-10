# The Route 53 hosted zone (var.domain_zone) is looked up via a data source rather than declared as a
# resource. This keeps Terraform's ownership boundary truthful: the stack can create / update / delete the
# single A record below, but it cannot create, modify, or destroy the zone itself or any unrelated records
# inside it (e.g., email/MX/TXT records that may share the same apex domain). The zone must already exist.

data "aws_route53_zone" "mc" {
  name = var.domain_zone
}

resource "aws_route53_record" "mc" {
  zone_id = data.aws_route53_zone.mc.zone_id
  name    = "${var.mc_subdomain}.${var.domain_zone}"
  type    = "A"
  ttl     = 300
  records = [aws_lightsail_static_ip.mcserver.ip_address]
}
