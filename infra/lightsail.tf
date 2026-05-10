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

# Operator's current public IPv4, fetched only when var.ssh_allowed_cidrs is empty.
# Used to populate the SSH allowlist with a /32 of the host running terraform.
data "http" "operator_ip" {
  count = length(var.ssh_allowed_cidrs) > 0 ? 0 : 1
  url   = "https://api.ipify.org"
}

locals {
  ssh_allowed_cidrs = length(var.ssh_allowed_cidrs) > 0 ? var.ssh_allowed_cidrs : ["${chomp(data.http.operator_ip[0].response_body)}/32"]
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

  # SSH is restricted to AWS's lightsail-connect range plus the operator's current IPv4 (or
  # var.ssh_allowed_cidrs when set). IPv6 SSH is intentionally not opened — dualstack would otherwise
  # widen this rule to every IPv6 host on the internet.
  port_info {
    protocol          = "tcp"
    from_port         = 22
    to_port           = 22
    cidrs             = local.ssh_allowed_cidrs
    cidr_list_aliases = ["lightsail-connect"]
  }
}
