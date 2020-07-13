import logging
import struct
import textwrap

from .exceptions import TockLoaderException

class TabApp:
	'''
	Representation of a Tock app for a specific board from a TAB file. This is
	different from a TAB, since a TAB can include compiled binaries for a range
	of architectures, or compiled for various scenarios, which may not be
	applicable for a particular board.

	A TabApp need not be a single (TBF header, binary) pair, as an app from a
	TAB can include multiple (header, binary) pairs if the app was compiled
	multiple times. This could be for any reason (e.g. it was signed with
	different keys, or it uses different compiler optimizations), but typically
	this is because it is compiled for specific addresses in flash and RAM, and
	there are multiple linked versions present in the TAB. If so, there will be
	multiple (header, binary) pairs included in this App object, and the correct
	one for the board will be used later.
	'''

	def __init__ (self, tbfs):
		'''
		Create a `TabApp` from a list of (TBF header, app binary) pairs.
		'''
		self.tbfs = tbfs # A list of (TBF header, app binary) pairs.

	def get_name (self):
		'''
		Return the app name.
		'''
		app_names = set([tbf[0].get_app_name() for tbf in self.tbfs])
		if len(app_names) > 1:
			raise TockLoaderException('Different names inside the same TAB?')
		elif len(app_names) == 0:
			raise TockLoaderException('No name in the TBF binaries')

		return app_names.pop()

	def is_modified (self):
		'''
		Returns whether this app needs to be flashed on to the board. Since this
		is a TabApp, we did not get this app from the board and therefore we
		have to flash this to the board.
		'''
		return True

	def set_sticky (self):
		'''
		Mark this app as "sticky" in the app's header. This makes it harder to
		accidentally remove this app if it is a core service or debug app.
		'''
		for tbfh,binary in self.tbfs:
			tbfh.set_flag('sticky', True)

	def get_header (self):
		'''
		Return a header if there is only one.
		'''
		if len(self.tbfs) == 1:
			return self.tbfs[0][0]
		return None

	def get_size (self):
		'''
		Return the total size (including TBF header) of this app in bytes.
		'''
		app_sizes = set([tbf[0].get_app_size() for tbf in self.tbfs])
		if len(app_sizes) > 1:
			raise TockLoaderException('Different app sizes inside the same TAB?')
		elif len(app_sizes) == 0:
			raise TockLoaderException('No TBF apps')

		return app_sizes.pop()

	def set_size (self, size):
		'''
		Force the entire app to be a certain size. If `size` is smaller than the
		actual app an error will be thrown.
		'''
		for tbfh,app_binary in self.tbfs:
			header_size = tbfh.get_header_size()
			binary_size = len(app_binary)
			current_size = header_size + binary_size
			if size < current_size:
				raise TockLoaderException('Cannot make app smaller. Current size: {} bytes'.format(current_size))
			tbfh.set_app_size(size)

	def has_fixed_addresses(self):
		'''
		Return true if any TBF binary in this app is compiled for a fixed
		address. That likely implies _all_ binaries are compiled for a fixed
		address.
		'''
		has_fixed_addresses = False
		for tbfh,app_binary in self.tbfs:
			if tbfh.has_fixed_addresses():
				has_fixed_addresses = True
				break
		return has_fixed_addresses

	def is_loadable_at_address (self, address):
		'''
		Check if it is possible to load this app at the given address. Returns
		True if it is possible, False otherwise.
		'''
		if not self.has_fixed_addresses():
			# No fixed addresses means we can put the app anywhere.
			return True

		# Otherwise, see if we have a TBF which can go at the requested address.
		for tbfh,app_binary in self.tbfs:
			fixed_flash_address = tbfh.get_fixed_addresses()[1]
			tbf_header_length = tbfh.get_header_size()

			# Ok, we have to be a little tricky here. What we actually care
			# about is ensuring that the application binary itself ends up at
			# the requested fixed address. However, what this function has to do
			# is see if the start of the TBF header can go at the requested
			# address. We have some flexibility, since we can make the header
			# larger so that it pushes the application binary to the correct
			# address. So, we want to see if we can reasonably do that. If we
			# are within 128 bytes, we say that we can.
			if fixed_flash_address >= (address + tbf_header_length) and\
			   (address + tbf_header_length + 128) > fixed_flash_address:
			    return True

		return False

	def get_next_loadable_address (self, address):
		'''
		Calculate the next reasonable address where we can put this app where
		the address is greater than or equal to `address`. The `address`
		argument is the earliest address the app can be at, either the start of
		apps or immediately after a previous app.

		If the app doesn't have a fixed address, then we can put it anywhere,
		and we just return the address. If the app is compiled with fixed
		addresses, then we need to calculate an address. We do a little bit of
		"reasonable assuming" here. Fixed addresses are based on where the _app
		binary_ must be located. Therefore, the start of the app where the TBF
		header goes must be before that. This can be at any address (as long as
		the header will fit), but we want to make this simpler, so we just
		assume the TBF header should start on a 1024 byte alignment.
		'''
		if not self.has_fixed_addresses():
			# No fixed addresses means we can put the app anywhere.
			return address

		def align_down_to(v, a):
			'''
			Calculate the address correctly aligned to `a` that is lower than or
			equal to `v`.
			'''
			return v - (v % a)

		# Find the binary with the lowest valid address that is above `address`.
		best = None
		for tbfh,app_binary in self.tbfs:
			fixed_flash_address = tbfh.get_fixed_addresses()[1]

			# Align to get a reasonable address for this app.
			wanted_address = align_down_to(fixed_flash_address, 1024)

			if wanted_address >= address:
				if best == None:
					best = wanted_address
				elif wanted_address < best:
					best = wanted_address

		return best

	def has_app_binary (self):
		'''
		Return true if we have an application binary with this app.
		'''
		# By definition, a TabApp will have an app binary.
		return True

	def get_binary (self, address):
		'''
		Return the binary array comprising the entire application.

		`address` is the address of flash the _start_ of the app will be placed
		at. This means where the TBF header will go.
		'''
		# See if there is binary that we have that matches the address
		# requirement.
		binary = None
		for tbfh,app_binary in self.tbfs:
			# If the TBF is not compiled for a fixed address, then we can just
			# use it.
			if tbfh.has_fixed_addresses() == False:
				binary = tbfh.get_binary() + app_binary
				break

			else:
				# Check the fixed address, and see if the TBF header ends up at
				# the correct address.
				fixed_flash_address = tbfh.get_fixed_addresses()[1]
				tbf_header_length = tbfh.get_header_size()

				if fixed_flash_address >= (address + tbf_header_length) and\
				   (address + tbf_header_length + 128) > fixed_flash_address:
					# Ok, this is the TBF that matched.

					tbfh.adjust_starting_address(address)

					binary = tbfh.get_binary() + app_binary
					break

		# We didn't find a matching binary.
		if binary == None:
			return None

		# Check that the binary is not longer than it is supposed to be. This
		# might happen if the size was changed, but any code using this binary
		# has no way to check. If the binary is too long, we truncate the actual
		# binary blob (which should just be padding) to the correct length. If
		# it is too short it is ok, since the board shouldn't care what is in
		# the flash memory the app is not using.
		size = self.get_size()
		if len(binary) > size:
			logging.debug('Binary is larger than what it says in the header. Actual:{}, expected:{}'
				.format(len(binary), size))
			logging.debug('Truncating binary to match.')

			# Check on what we would be removing. If it is all zeros, we
			# determine that it is OK to truncate.
			to_remove = binary[size:]
			if len(to_remove) != to_remove.count(0):
				raise TockLoaderException('Error truncating binary. Not zero.')

			binary = binary[0:size]

		return binary

	def get_crt0_header_str (self):
		'''
		Return a string representation of the crt0 header some apps use for
		doing PIC fixups. We assume this header is positioned immediately
		after the TBF header (AKA at the beginning of the application binary).
		'''
		tbfh,app_binary = self.tbfs[0]

		crt0 = struct.unpack('<IIIIIIIIII', app_binary[0:40])

		# Also display the number of relocations in the binary.
		reldata_start = crt0[8]
		reldata_len = struct.unpack('<I', app_binary[reldata_start:reldata_start+4])[0]

		out = ''
		out += '{:<20}: {:>10} {:>#12x}\n'.format('got_sym_start', crt0[0], crt0[0])
		out += '{:<20}: {:>10} {:>#12x}\n'.format('got_start', crt0[1], crt0[1])
		out += '{:<20}: {:>10} {:>#12x}\n'.format('got_size', crt0[2], crt0[2])
		out += '{:<20}: {:>10} {:>#12x}\n'.format('data_sym_start', crt0[3], crt0[3])
		out += '{:<20}: {:>10} {:>#12x}\n'.format('data_start', crt0[4], crt0[4])
		out += '{:<20}: {:>10} {:>#12x}\n'.format('data_size', crt0[5], crt0[5])
		out += '{:<20}: {:>10} {:>#12x}\n'.format('bss_start', crt0[6], crt0[6])
		out += '{:<20}: {:>10} {:>#12x}\n'.format('bss_size', crt0[7], crt0[7])
		out += '{:<20}: {:>10} {:>#12x}\n'.format('reldata_start', crt0[8], crt0[8])
		out += '  {:<18}: {:>10} {:>#12x}\n'.format('[reldata_len]', reldata_len, reldata_len)
		out += '{:<20}: {:>10} {:>#12x}\n'.format('stack_size', crt0[9], crt0[9])

		return out

	def __str__ (self):
		return self.get_name()
