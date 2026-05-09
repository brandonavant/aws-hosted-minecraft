# The bytehorizonforge.com hosted zone pre-existed this project and hosts records for
# unrelated concerns (an Azure M365 email setup under auth.*). It is therefore looked up
# via a data source rather than imported as a resource — this Terraform does not own the
# zone, only the single record that points the Minecraft DNS at the LightSail static IP.

data "aws_route53_zone" "bytehorizonforge" {
  name = "bytehorizonforge.com"
}

resource "aws_route53_record" "mc" {
  zone_id = data.aws_route53_zone.bytehorizonforge.zone_id
  name    = "mc.bytehorizonforge.com"
  type    = "A"
  ttl     = 300
  records = [aws_lightsail_static_ip.mcserver.ip_address]
}
