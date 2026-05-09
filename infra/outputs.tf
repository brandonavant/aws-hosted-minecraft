output "mcserver_public_ip" {
  description = "Static IPv4 address attached to the Minecraft server."
  value       = aws_lightsail_static_ip.mcserver.ip_address
}

output "mcserver_dns_name" {
  description = "Public DNS name players connect to (Java edition)."
  value       = aws_route53_record.mc.fqdn
}
