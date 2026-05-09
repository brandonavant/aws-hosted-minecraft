resource "aws_lightsail_instance" "mcserver" {
  name              = "mcserver-prod"
  availability_zone = "us-east-1a"
  blueprint_id      = "ubuntu_22_04"
  bundle_id         = "large_3_0"
  key_pair_name     = "ssh-mcserver"
  ip_address_type   = "dualstack"

  add_on {
    type          = "AutoSnapshot"
    snapshot_time = "11:00"
    status        = "Enabled"
  }
}

resource "aws_lightsail_instance_public_ports" "mcserver" {
  instance_name = aws_lightsail_instance.mcserver.name

  port_info {
    protocol   = "tcp"
    from_port  = 80
    to_port    = 80
    ipv6_cidrs = ["::/0"]
  }

  port_info {
    protocol   = "tcp"
    from_port  = 25565
    to_port    = 25565
    cidrs      = ["0.0.0.0/0"]
    ipv6_cidrs = ["::/0"]
  }

  port_info {
    protocol  = "udp"
    from_port = 19132
    to_port   = 19132
    cidrs     = ["0.0.0.0/0"]
  }

  port_info {
    protocol   = "tcp"
    from_port  = 19132
    to_port    = 19132
    ipv6_cidrs = ["::/0"]
  }

  port_info {
    protocol          = "tcp"
    from_port         = 22
    to_port           = 22
    cidrs             = ["198.148.30.0/32"]
    ipv6_cidrs        = ["::/0"]
    cidr_list_aliases = ["lightsail-connect"]
  }
}
