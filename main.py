import socket
from time import sleep
from struct import pack

from packets import TcpUsbHeader

DFU_DETACH_SETUP = pack(
    "<BBHHH", 
    0x21,       # bmRequestType (Class, Interface, Host-to-Device)
    0x00,       # bRequest (DFU_DETACH)
    0x03E8,     # wValue (Timeout: 1000 ms)
    0x0000,     # wIndex (Interface 0)
    0x0000      # wLength (No data)
)

GET_DFU_STATE = pack(
    "<BBHHH",
    0xA1,       # bmRequestType: Device-to-Host, Class, Interface
    0x05,       # bRequest: DFU_GETSTATE
    0x0000,     # wValue: not used in GETSTATE
    0x0000,     # wIndex: Interface 0
    0x0100      # wLength: 1 byte (requested)
)

GET_DFU_STATUS = pack(
    "<BBHHH",
    0xA1,       # bmRequestType: Device-to-Host, Class, Interface
    0x03,       # bRequest: DFU_GETSTATUS
    0x0000,     # wValue: not used in GETSTATUS
    0x0000,     # wIndex: Interface 0
    0x0600      # wLength: 1 byte (requested)
)

USB_DIR_IN = 0x80

# Create a TCP/IP socket
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

# Bind the socket to the address and port
server_address = ('localhost', 1235)
server_socket.bind(server_address)

# Listen for incoming connections
server_socket.listen(1)

while True:
    # Wait for a connection
    print('Waiting for a connection...')
    connection, client_address = server_socket.accept()

    try:
        print(f'Connection from {client_address}')
        sleep(2)

        # header_setup = TcpUsbHeader(addr=0x01, ep=0x00, flags=0x01, length=len(DFU_DETACH_SETUP))
        # connection.send(header_setup.pack() + DFU_DETACH_SETUP)
        # print("Setup packet sent.")
        # sleep(0.1)

        header_setup = TcpUsbHeader(addr=0x01, ep=0x00, flags=0x01, length=len(GET_DFU_STATE))
        connection.send(header_setup.pack() + GET_DFU_STATE)
        print("Setup packet sent.")

        # # Now, for a GET_DESCRIPTOR the host must issue an IN token.
        # # Typically the IN token is sent on endpoint 0 with the IN direction bit set.
        # # (In many USB stacks, the IN direction is indicated by bit 7 in the endpoint field.)
        sleep(0.2)  # A small delay might be needed in your simulation
        header_in = TcpUsbHeader(addr=0x01, ep=(0x00 | USB_DIR_IN), flags=0x00, length=1)
        connection.send(header_in.pack())
        print("IN token sent.")

        # Receive the data in small chunks and print it
        while True:
            data = connection.recv(16)
            print(f"DFU Descriptor: {data.hex()}")

            if not data:
                print('No more data from', client_address)
                break


        sleep(0.2)  # A small delay might be needed in your simulation
        header_in = TcpUsbHeader(addr=0x01, ep=(0x00 | USB_DIR_IN), flags=0x00, length=1)
        connection.send(header_in.pack())
        print("IN token sent.")

        # Receive the data in small chunks and print it
        while True:
            data = connection.recv(16)
            print(f"DFU Descriptor: {data.hex()}")

            if not data:
                print('No more data from', client_address)
                break

    finally:
        # Clean up the connection
        connection.close()
        break
