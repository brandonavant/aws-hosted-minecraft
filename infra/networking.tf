resource "aws_lightsail_static_ip" "mcserver" {
  name = "ip-mcserver-prod"
}

resource "aws_lightsail_static_ip_attachment" "mcserver" {
  static_ip_name = aws_lightsail_static_ip.mcserver.name
  instance_name  = aws_lightsail_instance.mcserver.name
}
