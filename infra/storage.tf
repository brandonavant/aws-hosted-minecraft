resource "aws_lightsail_disk" "mcserver_data" {
  name              = "disk-mcserver-prod"
  size_in_gb        = 128
  availability_zone = "us-east-1a"
}

resource "aws_lightsail_disk_attachment" "mcserver_data" {
  disk_name     = aws_lightsail_disk.mcserver_data.name
  instance_name = aws_lightsail_instance.mcserver.name
  disk_path     = "/dev/xvdf"
}
